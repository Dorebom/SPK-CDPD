from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .models import (
    ChatIdentity,
    FailureKind,
    Message,
    MessageIdentity,
    MessageRole,
    MessageSnapshot,
    WebDebugEvidence,
)
from .session import ChatGPTSession, _identity_of


_MESSAGE_OBSERVE_SCRIPT = r"""
() => {
  /* SHIRABE_MESSAGE_OBSERVE_V1 */
  const articleTurns = [...document.querySelectorAll(
    'article[data-testid^="conversation-turn-"], article[data-message-author-role]'
  )];
  const roleTurns = articleTurns.length ? [] : [...document.querySelectorAll('[data-message-author-role]')];
  const unique = [...new Set(articleTurns.length ? articleTurns : roleTurns)];
  const rows = [];
  for (const node of unique) {
    const roleNode = node.matches('[data-message-author-role]')
      ? node
      : node.querySelector('[data-message-author-role]');
    let role = roleNode ? roleNode.getAttribute('data-message-author-role') : null;
    if (!role) {
      const label = String(node.getAttribute('aria-label') || '').toLowerCase();
      if (label.includes('assistant')) role = 'assistant';
      else if (label.includes('you') || label.includes('user')) role = 'user';
    }
    role = String(role || '').toLowerCase();
    if (role !== 'user' && role !== 'assistant') continue;

    const body = node.querySelector('.markdown, [data-message-author-role] .markdown, [data-message-content]') || roleNode || node;
    let text = String(body.innerText || body.textContent || '');
    text = text.replace(/\u200B/g, '').trim();
    const rawId = node.getAttribute('data-message-id')
      || (roleNode && roleNode.getAttribute('data-message-id'))
      || null;
    rows.push({role, text, rawId});
  }
  return {url: location.href, rows};
}
"""

_PROJECT_KEY_RE = re.compile(r"^(g-p-[A-Za-z0-9]+)")


@dataclass(frozen=True)
class MessageObservationFailure:
    failure: FailureKind
    reason: str
    evidence: WebDebugEvidence


def observe_message_snapshot(
    session: ChatGPTSession,
    *,
    expected_chat: ChatIdentity | None = None,
) -> MessageSnapshot | MessageObservationFailure:
    """Observe user/assistant messages on the currently bound ChatGPT page.

    Message identity is based on a stable DOM-id fingerprint when available plus
    a semantic text hash and role-local index.  This mirrors the proven legacy
    monitor/downloader binding strategy without importing legacy packages.
    """

    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        if not bind.ok:
            return MessageObservationFailure(
                bind.failure or FailureKind.SESSION_BINDING_FAILURE,
                bind.reason or "ChatGPT page binding failed.",
                bind.evidence or WebDebugEvidence("messages.observe", ("bind=failed",)),
            )
        bound = session._bound_page()
    if bound is None:
        return MessageObservationFailure(
            FailureKind.SESSION_BINDING_FAILURE,
            "Session reported bound but no internal page exists.",
            WebDebugEvidence("messages.observe", ("bound-page=missing",)),
        )

    try:
        raw = bound.ops.evaluate(_MESSAGE_OBSERVE_SCRIPT) or {}
    except Exception as exc:
        return MessageObservationFailure(
            FailureKind.UNKNOWN_WEB_STATE,
            f"Message observation failed: {type(exc).__name__}: {exc}",
            WebDebugEvidence("messages.observe", ("message-observation=failed",)),
        )

    url = str(raw.get("url") or bound.ops.url)
    project_key, conversation_id = _route_identity(url)
    if expected_chat is not None:
        if project_key != expected_chat.project_key:
            return MessageObservationFailure(
                FailureKind.TARGET_IDENTITY_MISMATCH,
                "Observed message page does not belong to the expected Project.",
                WebDebugEvidence(
                    "messages.observe",
                    (f"expected_project_key={expected_chat.project_key}", f"observed_project_key={project_key}"),
                ),
            )
        if expected_chat.conversation_id is not None and conversation_id != expected_chat.conversation_id:
            return MessageObservationFailure(
                FailureKind.TARGET_IDENTITY_MISMATCH,
                "Observed message page does not match the expected conversation id.",
                WebDebugEvidence(
                    "messages.observe",
                    (
                        f"expected_conversation_id={expected_chat.conversation_id}",
                        f"observed_conversation_id={conversation_id}",
                    ),
                ),
            )

    rows = raw.get("rows") or []
    if not isinstance(rows, list):
        return MessageObservationFailure(
            FailureKind.UNKNOWN_WEB_STATE,
            "Message observation returned a non-list row payload.",
            WebDebugEvidence("messages.observe", ("rows=invalid",)),
        )

    messages: list[Message] = []
    role_counts = {MessageRole.USER: 0, MessageRole.ASSISTANT: 0}
    for row in rows:
        if not isinstance(row, dict):
            continue
        role_raw = str(row.get("role") or "").lower()
        if role_raw == "user":
            role = MessageRole.USER
        elif role_raw == "assistant":
            role = MessageRole.ASSISTANT
        else:
            continue
        text = _normalize_semantic_text(str(row.get("text") or ""))
        role_index = role_counts[role]
        role_counts[role] += 1
        raw_id = row.get("rawId")
        identity = MessageIdentity(
            project_key=project_key or (expected_chat.project_key if expected_chat else ""),
            conversation_id=conversation_id,
            role=role,
            role_index=role_index,
            semantic_text_sha256=_sha256(text),
            message_id_sha256=_sha256(str(raw_id)) if raw_id else None,
        )
        messages.append(Message(identity=identity, text=text))

    if not project_key and expected_chat is None:
        return MessageObservationFailure(
            FailureKind.UNKNOWN_WEB_STATE,
            "Project identity could not be established from the current message page.",
            WebDebugEvidence("messages.observe", (f"url={url}",)),
        )

    return MessageSnapshot(
        project_key=project_key or expected_chat.project_key,  # type: ignore[union-attr]
        conversation_id=conversation_id,
        messages=tuple(messages),
        user_count=role_counts[MessageRole.USER],
        assistant_count=role_counts[MessageRole.ASSISTANT],
    )


def latest_new_assistant_response(
    baseline: MessageSnapshot,
    current: MessageSnapshot,
) -> Message | None:
    """Return the assistant response that is new relative to a baseline.

    Count growth is preferred.  A changed latest semantic/message identity is a
    bounded fallback for virtualized DOM windows where the rendered count may
    remain constant while old turns leave the window.
    """

    latest = current.latest_assistant
    if latest is None:
        return None
    if current.assistant_count > baseline.assistant_count:
        return latest

    before = baseline.latest_assistant
    if before is None:
        return latest
    if _same_message_identity(before.identity, latest.identity):
        return None
    return latest


def message_matches_prompt(message: Message | None, prompt: str) -> bool:
    if message is None or message.identity.role != MessageRole.USER:
        return False
    return _normalize_semantic_text(message.text) == _normalize_semantic_text(prompt)


def promoted_chat_identity(original: ChatIdentity, snapshot: MessageSnapshot, *, url: str) -> ChatIdentity:
    """Promote a provisional Chat once the server conversation id is observable."""

    return ChatIdentity(
        project_key=original.project_key,
        url=url,
        conversation_id=snapshot.conversation_id,
        provisional_id=None if snapshot.conversation_id else original.provisional_id,
        client_ref=original.client_ref,
    )


def _same_message_identity(left: MessageIdentity, right: MessageIdentity) -> bool:
    if left.message_id_sha256 and right.message_id_sha256:
        return left.message_id_sha256 == right.message_id_sha256
    return (
        left.semantic_text_sha256 == right.semantic_text_sha256
        and left.role == right.role
        and left.role_index == right.role_index
    )


def _route_identity(url: str) -> tuple[str | None, str | None]:
    try:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        g_index = parts.index("g")
        segment = parts[g_index + 1]
        match = _PROJECT_KEY_RE.match(segment)
        project_key = match.group(1) if match else None
        conversation_id = None
        if g_index + 3 < len(parts) and parts[g_index + 2] == "c":
            conversation_id = parts[g_index + 3]
        return project_key, conversation_id
    except Exception:
        return None, None


def _normalize_semantic_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\u200b", "").strip().splitlines()).strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
