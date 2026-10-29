from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

from .composer import _composer_observations, _observe_composer
from .models import (
    ChatIdentity,
    ChatOpenResult,
    ComposerState,
    FailureKind,
    ProjectIdentity,
    WebDebugEvidence,
    WebOperationStatus,
)
from .session import ChatGPTSession, _identity_of


_PROJECT_KEY_RE = re.compile(r"(?P<key>g-p-[A-Za-z0-9]+)")
_CHAT_OBSERVE_SCRIPT = r"""
() => {
  /* SHIRABE_CHAT_OBSERVE_V1 */
  const parts = location.pathname.split('/').filter(Boolean);
  const g = parts.indexOf('g');
  const projectSegment = g >= 0 ? (parts[g + 1] || null) : null;
  const keyMatch = String(projectSegment || '').match(/(g-p-[A-Za-z0-9]+)/);
  let conversationId = null;
  if (g >= 0 && parts[g + 2] === 'c' && parts[g + 3]) conversationId = parts[g + 3];
  const messageNodes = document.querySelectorAll('[data-message-author-role], [data-testid^="conversation-turn-"]');
  const suffix = g >= 0 ? parts.slice(g + 2) : [];
  const isProjectNewChatRoute = Boolean(projectSegment)
    && conversationId === null
    && (suffix.length === 0 || (suffix.length === 1 && suffix[0] === 'project'));
  return {
    url: location.href,
    projectKey: keyMatch ? keyMatch[1] : null,
    projectSegment,
    conversationId,
    isProjectRoot: isProjectNewChatRoute,
    messageCount: messageNodes.length,
  };
}
"""


@dataclass(frozen=True)
class _ObservedChat:
    url: str
    project_key: str | None
    project_segment: str | None
    conversation_id: str | None
    is_project_root: bool
    message_count: int


def start_new_chat(
    session: ChatGPTSession,
    project: ProjectIdentity,
    *,
    client_ref: str | None = None,
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.25,
    provisional_id_factory: Callable[[], str] | None = None,
) -> ChatOpenResult:
    """Start a clean new Chat inside an already-verified Project.

    Success requires all postconditions at once:
    - current route belongs to the expected Project key;
    - route is the Project new-chat/root route (no conversation id yet);
    - zero rendered messages;
    - composer exists, is visible/editable, and is empty.

    The returned identity is intentionally provisional until ChatGPT allocates a
    server-side conversation id after the first prompt. Caller-specific role
    semantics (Execution/Assessment) remain outside this package; `client_ref` is
    only an opaque correlation value.
    """

    if not project.project_key:
        return _chat_failure(
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Project identity has no project key.",
            None,
            ["project-key=missing"],
        )

    bind = session.bind_chatgpt_page()
    if not bind.ok:
        return ChatOpenResult(
            status=WebOperationStatus.FAILED,
            chat=None,
            composer=None,
            page=bind.page,
            failure=bind.failure,
            reason=bind.reason,
            evidence=bind.evidence,
        )
    bound = session._bound_page()
    if bound is None:
        return _chat_failure(
            FailureKind.SESSION_BINDING_FAILURE,
            "Session reported bound but no internal page exists.",
            bind.page,
            ["bound-page=missing"],
        )

    root_url = _project_root_url(project)
    if root_url is None:
        return _chat_failure(
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Verified Project URL cannot be reduced to a Project root route.",
            _identity_of(bound.ops),
            [f"project-url={_safe_url(project.url)}", "new-chat-route=unresolved"],
        )

    observations = [
        f"expected_project_key={project.project_key}",
        f"new_chat_route={_safe_url(root_url)}",
    ]
    try:
        if _normalized_url(bound.ops.url) != _normalized_url(root_url):
            bound.ops.goto(root_url, timeout_ms=max(1, int(timeout_seconds * 1000)))
            observations.append("navigation=project-new-chat-route")
        else:
            observations.append("navigation=not-required")
    except Exception as exc:
        return _chat_failure(
            FailureKind.TIMEOUT if "timeout" in type(exc).__name__.lower() else FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            f"New Chat navigation failed: {type(exc).__name__}: {exc}",
            _identity_of(bound.ops),
            observations + ["navigation=failed"],
        )

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_chat: _ObservedChat | None = None
    last_composer: ComposerState | None = None
    last_candidate_count = 0
    last_error: str | None = None
    while True:
        try:
            last_chat = _observe_chat(bound.ops)
            last_composer, last_candidate_count = _observe_composer(bound.ops)
            failure = _new_chat_postcondition_failure(
                last_chat,
                last_composer,
                last_candidate_count,
                expected_project_key=project.project_key,
            )
            if failure is None:
                provisional_id = (provisional_id_factory or _new_provisional_id)()
                chat = ChatIdentity(
                    project_key=project.project_key,
                    url=last_chat.url,
                    conversation_id=None,
                    provisional_id=provisional_id,
                    client_ref=client_ref,
                )
                observations.extend(_chat_observations("after", last_chat))
                observations.extend(_composer_observations(last_composer, last_candidate_count))
                observations.extend(
                    (
                        "postcondition=project-identity-established",
                        "postcondition=new-chat-route-established",
                        "postcondition=zero-rendered-messages",
                        "postcondition=empty-composer-input-ready",
                    )
                )
                return ChatOpenResult(
                    status=WebOperationStatus.READY,
                    chat=chat,
                    composer=last_composer,
                    page=_identity_of(bound.ops),
                    evidence=WebDebugEvidence(
                        "chat.new",
                        tuple(observations),
                        {
                            "project_key": project.project_key,
                            "conversation_id": None,
                            "provisional_id": provisional_id,
                            "client_ref": client_ref,
                        },
                    ),
                )
            last_error = failure
        except Exception as exc:
            last_error = f"observation-error={type(exc).__name__}: {exc}"

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, poll_interval_seconds))

    if last_chat is not None:
        observations.extend(_chat_observations("after-timeout", last_chat))
    if last_composer is not None:
        observations.extend(_composer_observations(last_composer, last_candidate_count))
    if last_error:
        observations.append(last_error)

    kind = FailureKind.TARGET_IDENTITY_MISMATCH if last_chat and last_chat.project_key != project.project_key else FailureKind.POSTCONDITION_NOT_ESTABLISHED
    return _chat_failure(
        kind,
        "New Chat postconditions were not established; refusing to claim READY.",
        _identity_of(bound.ops),
        observations,
        composer=last_composer,
    )


def bind_chat(
    session: ChatGPTSession,
    chat: ChatIdentity,
    *,
    require_empty_composer: bool = True,
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.25,
) -> ChatOpenResult:
    """Bind a known durable conversation identity and verify its postcondition.

    Provisional new-chat identities are deliberately not restart-safe: there is
    no server conversation id with which to distinguish one empty Project root
    from another. Rebinding such an identity fails closed with RECONCILE_REQUIRED.
    """

    if chat.conversation_id is None:
        return _chat_failure(
            FailureKind.RECONCILE_REQUIRED,
            "A provisional Chat has no durable conversation id and cannot be safely rebound.",
            None,
            [f"provisional_id={chat.provisional_id}", "durable-conversation-id=missing"],
        )
    if not _identity_url_matches(chat.url, chat.project_key, chat.conversation_id):
        return _chat_failure(
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Chat identity URL does not encode the expected Project/conversation identity.",
            None,
            [f"chat-url={_safe_url(chat.url)}"],
        )

    bind = session.bind_chatgpt_page()
    if not bind.ok:
        return ChatOpenResult(
            status=WebOperationStatus.FAILED,
            chat=None,
            composer=None,
            page=bind.page,
            failure=bind.failure,
            reason=bind.reason,
            evidence=bind.evidence,
        )
    bound = session._bound_page()
    if bound is None:
        return _chat_failure(FailureKind.SESSION_BINDING_FAILURE, "Bound page is missing.", bind.page, [])

    observations = [
        f"expected_project_key={chat.project_key}",
        f"expected_conversation_id={chat.conversation_id}",
    ]
    try:
        if _normalized_url(bound.ops.url) != _normalized_url(chat.url):
            bound.ops.goto(chat.url, timeout_ms=max(1, int(timeout_seconds * 1000)))
            observations.append("navigation=known-chat-route")
    except Exception as exc:
        return _chat_failure(
            FailureKind.TIMEOUT if "timeout" in type(exc).__name__.lower() else FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            f"Known Chat navigation failed: {type(exc).__name__}: {exc}",
            _identity_of(bound.ops),
            observations + ["navigation=failed"],
        )

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_chat: _ObservedChat | None = None
    last_composer: ComposerState | None = None
    last_candidate_count = 0
    while True:
        try:
            last_chat = _observe_chat(bound.ops)
            last_composer, last_candidate_count = _observe_composer(bound.ops)
            identity_ok = (
                last_chat.project_key == chat.project_key
                and last_chat.conversation_id == chat.conversation_id
            )
            composer_ok = (
                last_candidate_count == 1
                and last_composer.input_ready
                and (last_composer.empty or not require_empty_composer)
            )
            if identity_ok and composer_ok:
                durable = ChatIdentity(
                    project_key=chat.project_key,
                    url=last_chat.url,
                    conversation_id=chat.conversation_id,
                    provisional_id=None,
                    client_ref=chat.client_ref,
                )
                observations.extend(_chat_observations("bound", last_chat))
                observations.extend(_composer_observations(last_composer, last_candidate_count))
                observations.append("postcondition=known-chat-identity-established")
                observations.append("postcondition=composer-input-ready")
                if require_empty_composer:
                    observations.append("postcondition=composer-empty")
                return ChatOpenResult(
                    status=WebOperationStatus.READY,
                    chat=durable,
                    composer=last_composer,
                    page=_identity_of(bound.ops),
                    evidence=WebDebugEvidence(
                        "chat.bind",
                        tuple(observations),
                        {
                            "verified_project_key": chat.project_key,
                            "verified_conversation_id": chat.conversation_id,
                            "require_empty_composer": require_empty_composer,
                        },
                    ),
                )
        except Exception as exc:
            observations.append(f"observation-error={type(exc).__name__}")

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, poll_interval_seconds))

    if last_chat is not None:
        observations.extend(_chat_observations("after-timeout", last_chat))
    if last_composer is not None:
        observations.extend(_composer_observations(last_composer, last_candidate_count))

    identity_mismatch = last_chat is not None and (
        last_chat.project_key != chat.project_key or last_chat.conversation_id != chat.conversation_id
    )
    return _chat_failure(
        FailureKind.TARGET_IDENTITY_MISMATCH if identity_mismatch else FailureKind.POSTCONDITION_NOT_ESTABLISHED,
        "Known Chat identity/readiness postcondition was not established.",
        _identity_of(bound.ops),
        observations,
        composer=last_composer,
    )


def _observe_chat(page: Any) -> _ObservedChat:
    raw = page.evaluate(_CHAT_OBSERVE_SCRIPT) or {}
    return _ObservedChat(
        url=str(raw.get("url") or page.url),
        project_key=_as_nonempty(raw.get("projectKey")) or _project_key_from_url(str(raw.get("url") or page.url)),
        project_segment=_as_nonempty(raw.get("projectSegment")),
        conversation_id=_as_nonempty(raw.get("conversationId")),
        is_project_root=bool(raw.get("isProjectRoot")),
        message_count=max(0, int(raw.get("messageCount") or 0)),
    )


def _new_chat_postcondition_failure(
    observed: _ObservedChat,
    composer: ComposerState,
    composer_candidate_count: int,
    *,
    expected_project_key: str,
) -> str | None:
    if observed.project_key != expected_project_key:
        return "project-identity-mismatch"
    if not _is_supported_project_new_chat_route(observed.url, expected_project_key):
        return "new-chat-route-not-established"
    if observed.conversation_id is not None:
        return "new-chat-route-not-established"
    if observed.message_count != 0:
        return "rendered-messages-not-zero"
    # S3 establishes semantic new-chat readiness, not a unique DOM selector.
    # The raw composer candidate count remains diagnostic evidence; actual
    # prompt submission keeps its stricter ambiguity gate in composer.py.
    if composer_candidate_count < 1:
        return "composer-not-observed"
    if not composer.input_ready:
        return "composer-not-input-ready"
    if not composer.empty:
        return "composer-not-empty"
    return None



def _is_supported_project_new_chat_route(url: str, expected_project_key: str) -> bool:
    """Return True only for Project-local, conversation-free New Chat routes.

    ChatGPT Web currently uses both the Project root and a ``/project``
    suffix for the same unsent Project Chat state. Query strings/fragments are
    irrelevant to route identity. Existing conversation routes (``/c/<id>``)
    and any other suffix remain rejected.
    """

    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}:
            return False
        parts = [part for part in parsed.path.split("/") if part]
        g = parts.index("g")
        if g + 1 >= len(parts):
            return False
        if _project_key_from_url(parts[g + 1]) != expected_project_key:
            return False
        suffix = parts[g + 2 :]
        return suffix == [] or suffix == ["project"]
    except Exception:
        return False

def _project_root_url(project: ProjectIdentity) -> str | None:
    try:
        parsed = urlparse(project.url)
        if parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}:
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if "g" not in parts:
            return None
        idx = parts.index("g")
        if idx + 1 >= len(parts):
            return None
        segment = parts[idx + 1]
        key = _project_key_from_url(segment)
        if key != project.project_key:
            return None
        return urlunparse((parsed.scheme or "https", parsed.netloc, f"/g/{segment}", "", "", ""))
    except Exception:
        return None


def _identity_url_matches(url: str, project_key: str, conversation_id: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}:
            return False
        parts = [p for p in parsed.path.split("/") if p]
        g = parts.index("g")
        return (
            g + 3 < len(parts)
            and _project_key_from_url(parts[g + 1]) == project_key
            and parts[g + 2] == "c"
            and parts[g + 3] == conversation_id
        )
    except Exception:
        return False


def _chat_observations(prefix: str, observed: _ObservedChat) -> list[str]:
    return [
        f"{prefix}.url={_safe_url(observed.url)}",
        f"{prefix}.project_key={observed.project_key}",
        f"{prefix}.conversation_id={observed.conversation_id}",
        f"{prefix}.is_project_root={observed.is_project_root}",
        f"{prefix}.message_count={observed.message_count}",
    ]


def _chat_failure(
    kind: FailureKind,
    reason: str,
    page: Any,
    observations: list[str],
    *,
    composer: ComposerState | None = None,
) -> ChatOpenResult:
    return ChatOpenResult(
        status=WebOperationStatus.FAILED,
        chat=None,
        composer=composer,
        page=page,
        failure=kind,
        reason=reason,
        evidence=WebDebugEvidence("chat.lifecycle", tuple(observations)),
    )


def _project_key_from_url(value: str) -> str | None:
    match = _PROJECT_KEY_RE.search(value)
    return match.group("key") if match else None


def _new_provisional_id() -> str:
    return f"provisional-{uuid4().hex}"


def _normalized_url(url: str) -> str:
    return url.rstrip("/")


def _safe_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed._replace(query="", fragment="").geturl()
    except Exception:
        return url


def _as_nonempty(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
