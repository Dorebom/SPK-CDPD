from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chatgpt_web_api.composer import submit_prompt
from chatgpt_web_api.generation import observe_generation, submit_prompt_and_wait_for_response
from chatgpt_web_api.messages import MessageObservationFailure, observe_message_snapshot
from chatgpt_web_api.models import (
    ChatIdentity,
    FailureKind,
    GenerationState,
    MessageRole,
    WebOperationStatus,
)
from chatgpt_web_api.session import ChatGPTSession
from strict_fakes import make_bound_session


@dataclass
class FakePage:
    _url: str
    focused: bool = True
    _target_id: str = "target-1"
    composer_observations: list[dict[str, Any]] = field(default_factory=list)
    message_observations: list[dict[str, Any]] = field(default_factory=list)
    generation_observations: list[dict[str, Any]] = field(default_factory=list)
    submit_actions: list[dict[str, Any]] = field(default_factory=list)
    generation_probe_installs: int = 0

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
        if "SHIRABE_COMPOSER_OBSERVE_V1" in expression:
            return _next(self.composer_observations)
        if "SHIRABE_MESSAGE_OBSERVE_V1" in expression:
            row = dict(_next(self.message_observations))
            if row.get("url"):
                self._url = row["url"]
            return row
        if "SHIRABE_PROMPT_INPUT_V1" in expression or "SHIRABE_SEND_ACTION_SELECT_V2" in expression:
            return _next(self.submit_actions) if self.submit_actions else {"ok": True, "reason": "send-action-selected"}
        if "SHIRABE_GENERATION_PROBE_INSTALL_V1" in expression:
            self.generation_probe_installs += 1
            return {"installed": True, "existing": self.generation_probe_installs > 1}
        if "SHIRABE_GENERATION_OBSERVE_V1" in expression:
            return _next(self.generation_observations)
        raise AssertionError(f"unexpected evaluate script: {expression[:100]}")

    def trusted_click_marked_action(self, selector: str, *, timeout_ms: int) -> dict[str, Any]:
        return {
            "ok": True, "reason": "trusted-click-delivered",
            "activationAttempted": True, "trustedClick": True,
            "locatorCount": 1, "trustedClickEventCount": 1, "clickEventCount": 1,
        }

    def goto(self, url: str, *, timeout_ms: int) -> None:
        self._url = url


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


def composer(*, empty: bool = True, editable: bool = True, count: int = 1) -> dict[str, Any]:
    return {
        "present": count > 0,
        "visible": count > 0,
        "editable": editable,
        "empty": empty,
        "textLength": 0 if empty else 5,
        "candidateCount": count,
    }


def route(conv: str = "conv-1") -> str:
    return f"https://chatgpt.com/g/g-p-abc123-spk-shirabe/c/{conv}"


def messages(*rows: tuple[str, str, str | None], url: str | None = None) -> dict[str, Any]:
    return {
        "url": url or route(),
        "rows": [{"role": role, "text": text, "rawId": raw_id} for role, text, raw_id in rows],
    }


def gen(*, stop: bool = False, stream: bool = False, user_stop: bool = False) -> dict[str, Any]:
    return {
        "stopVisible": stop,
        "streamingVisible": stream,
        "userStopRequested": user_stop,
        "stopClickCount": 1 if user_stop else 0,
    }


def chat(*, durable: bool = True) -> ChatIdentity:
    return ChatIdentity(
        project_key="g-p-abc123",
        url=route() if durable else "https://chatgpt.com/g/g-p-abc123-spk-shirabe",
        conversation_id="conv-1" if durable else None,
        provisional_id=None if durable else "provisional-1",
        client_ref="execution",
    )


def test_message_snapshot_builds_stable_fingerprints_and_role_indexes() -> None:
    page = FakePage(
        route(),
        message_observations=[messages(("user", "hello", "u-1"), ("assistant", "world", "a-1"))],
    )
    result = observe_message_snapshot(make_bound_session(transport=FakeTransport([page])), expected_chat=chat())
    assert not isinstance(result, MessageObservationFailure)
    assert result.user_count == 1 and result.assistant_count == 1
    assert result.latest_user is not None and result.latest_user.identity.role == MessageRole.USER
    assert result.latest_assistant is not None and result.latest_assistant.identity.role_index == 0
    assert result.latest_assistant.identity.message_id_sha256 is not None


def test_message_snapshot_rejects_wrong_conversation_identity() -> None:
    page = FakePage(route("wrong"), message_observations=[messages(url=route("wrong"))])
    result = observe_message_snapshot(make_bound_session(transport=FakeTransport([page])), expected_chat=chat())
    assert isinstance(result, MessageObservationFailure)
    assert result.failure == FailureKind.TARGET_IDENTITY_MISMATCH


def test_submit_prompt_requires_sent_message_and_empty_composer_postcondition() -> None:
    prompt = "implement ticket"
    page = FakePage(
        route(),
        composer_observations=[composer(empty=True), composer(empty=True)],
        message_observations=[
            messages(("user", "old", "u-0")),
            messages(("user", "old", "u-0"), ("user", prompt, "u-1")),
        ],
        submit_actions=[{"ok": True, "reason": "action-dispatched"}],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])), chat(), prompt, timeout_seconds=0
    )
    assert result.ok
    assert result.status == WebOperationStatus.SUBMITTED
    assert result.user_message is not None and result.user_message.text == prompt


def test_submit_prompt_does_not_treat_click_action_as_success_without_message_postcondition() -> None:
    page = FakePage(
        route(),
        composer_observations=[composer(empty=True)],
        message_observations=[messages(), messages()],
        submit_actions=[{"ok": True, "reason": "action-dispatched"}],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])), chat(), "hello", timeout_seconds=0
    )
    assert not result.ok
    assert result.failure == FailureKind.TIMEOUT


def test_observe_generation_distinguishes_running_and_user_stopped() -> None:
    page = FakePage(route(), generation_observations=[gen(stop=True), gen(user_stop=True)])
    session = make_bound_session(transport=FakeTransport([page]))
    running = observe_generation(session)
    stopped = observe_generation(session)
    assert running.state == GenerationState.RUNNING
    assert stopped.state == GenerationState.USER_STOPPED


def test_end_to_end_prompt_operation_completes_only_after_new_response_is_stable_and_generation_stops() -> None:
    prompt = "do work"
    # message observation order:
    # 1 high-level baseline
    # 2 submit baseline
    # 3 submit postcondition
    # then 4.. generation polls.
    page = FakePage(
        route(),
        composer_observations=[composer(), composer()],
        message_observations=[
            messages(),
            messages(),
            messages(("user", prompt, "u-1")),
            messages(("user", prompt, "u-1"), ("assistant", "part", "a-1")),
            messages(("user", prompt, "u-1"), ("assistant", "done", "a-1")),
            messages(("user", prompt, "u-1"), ("assistant", "done", "a-1")),
            messages(("user", prompt, "u-1"), ("assistant", "done", "a-1")),
        ],
        generation_observations=[gen(stop=True), gen(stream=True), gen(), gen()],
    )
    result = submit_prompt_and_wait_for_response(
        make_bound_session(transport=FakeTransport([page])),
        chat(),
        prompt,
        submit_timeout_seconds=0,
        generation_timeout_seconds=1,
        poll_interval_seconds=0.001,
        stable_samples_required=2,
    )
    assert result.completed
    assert result.generation_state == GenerationState.COMPLETED
    assert result.response is not None and result.response.text == "done"
    assert result.response.identity.message_id_sha256 is not None
    assert result.chat is not None and result.chat.conversation_id == "conv-1"


def test_end_to_end_operation_returns_user_stopped_separately_with_partial_response_identity() -> None:
    prompt = "long work"
    page = FakePage(
        route(),
        composer_observations=[composer(), composer()],
        message_observations=[
            messages(),
            messages(),
            messages(("user", prompt, "u-1")),
            messages(("user", prompt, "u-1"), ("assistant", "partial", "a-1")),
            messages(("user", prompt, "u-1"), ("assistant", "partial", "a-1")),
        ],
        generation_observations=[gen(stop=True), gen(user_stop=True)],
    )
    result = submit_prompt_and_wait_for_response(
        make_bound_session(transport=FakeTransport([page])),
        chat(),
        prompt,
        submit_timeout_seconds=0,
        generation_timeout_seconds=1,
        poll_interval_seconds=0.001,
        stable_samples_required=1,
    )
    assert result.user_stopped
    assert result.generation_state == GenerationState.USER_STOPPED
    assert result.failure == FailureKind.USER_STOPPED
    assert result.response is not None and result.response.text == "partial"


def test_end_to_end_operation_times_out_if_no_assistant_response_postcondition_exists() -> None:
    prompt = "work"
    page = FakePage(
        route(),
        composer_observations=[composer(), composer()],
        message_observations=[
            messages(),
            messages(),
            messages(("user", prompt, "u-1")),
            messages(("user", prompt, "u-1")),
        ],
        generation_observations=[gen(stop=True)],
    )
    result = submit_prompt_and_wait_for_response(
        make_bound_session(transport=FakeTransport([page])),
        chat(),
        prompt,
        submit_timeout_seconds=0,
        generation_timeout_seconds=0,
        stable_samples_required=1,
    )
    assert not result.completed
    assert result.generation_state == GenerationState.TIMEOUT
    assert result.failure == FailureKind.TIMEOUT


def test_provisional_chat_is_promoted_when_submission_route_allocates_conversation_id() -> None:
    prompt = "hello"
    page = FakePage(
        "https://chatgpt.com/g/g-p-abc123-spk-shirabe",
        composer_observations=[composer(), composer()],
        message_observations=[
            messages(url="https://chatgpt.com/g/g-p-abc123-spk-shirabe"),
            messages(("user", prompt, "u-1"), url=route("new-conv")),
        ],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat(durable=False),
        prompt,
        timeout_seconds=0,
    )
    assert result.ok
    assert result.chat is not None
    assert result.chat.conversation_id == "new-conv"
    assert result.chat.provisional_id is None



def test_submit_prompt_accepts_empty_composer_even_if_it_becomes_temporarily_noneditable_during_generation() -> None:
    prompt = "start generation"
    page = FakePage(
        route(),
        composer_observations=[composer(empty=True), composer(empty=True, editable=False)],
        message_observations=[messages(), messages(("user", prompt, "u-1"))],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])), chat(), prompt, timeout_seconds=0
    )
    assert result.ok


def test_user_stopped_is_terminal_even_if_no_partial_assistant_body_was_rendered() -> None:
    prompt = "stop immediately"
    page = FakePage(
        route(),
        composer_observations=[composer(), composer()],
        message_observations=[
            messages(),
            messages(),
            messages(("user", prompt, "u-1")),
            messages(("user", prompt, "u-1")),
        ],
        generation_observations=[gen(user_stop=True)],
    )
    result = submit_prompt_and_wait_for_response(
        make_bound_session(transport=FakeTransport([page])),
        chat(),
        prompt,
        submit_timeout_seconds=0,
        generation_timeout_seconds=1,
        stable_samples_required=1,
    )
    assert result.user_stopped
    assert result.response is None


def test_web003_modules_do_not_import_shirabe_runtime() -> None:
    from pathlib import Path

    root = (Path(__file__).resolve().parents[1] / "src" / "chatgpt_web_api")
    for name in ("models.py", "composer.py", "messages.py", "generation.py"):
        assert "shirabe_runtime" not in (root / name).read_text(encoding="utf-8")


def test_attachment_failure_blocks_prompt_submission_before_text_dispatch(monkeypatch) -> None:
    import chatgpt_web_api.generation as generation_module
    from chatgpt_web_api.models import ComposerAttachmentResult, WebDebugEvidence

    prompt = "short instruction"
    page = FakePage(
        route(),
        message_observations=[messages()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    submission_calls: list[str] = []

    monkeypatch.setattr(
        generation_module,
        "attach_files",
        lambda *args, **kwargs: ComposerAttachmentResult(
            status=WebOperationStatus.FAILED,
            filenames=("context.md",),
            page=None,
            failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            reason="attachment not observable",
            evidence=WebDebugEvidence("composer.attach", ("attachment_postcondition=missing",)),
        ),
    )
    monkeypatch.setattr(
        generation_module,
        "submit_prompt",
        lambda *args, **kwargs: submission_calls.append("submitted"),
    )

    result = generation_module.submit_prompt_and_wait_for_response(
        session,
        chat(),
        prompt,
        attachments=(
            {
                "path": "/tmp/context.md",
                "filename": "context.md",
                "sha256": "a" * 64,
                "media_type": "text/markdown",
                "size_bytes": 1,
            },
        ),
        submit_timeout_seconds=0,
        generation_timeout_seconds=0,
        stable_samples_required=1,
    )

    assert result.generation_state == GenerationState.NOT_STARTED
    assert result.failure == FailureKind.POSTCONDITION_NOT_ESTABLISHED
    assert submission_calls == []
