from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .composer import attach_files, submit_prompt
from .messages import MessageObservationFailure, latest_new_assistant_response, observe_message_snapshot
from .models import (
    AssistantResponse,
    ChatIdentity,
    FailureKind,
    GenerationObservation,
    GenerationState,
    MessageSnapshot,
    PromptResponseResult,
    WebDebugEvidence,
    WebOperationStatus,
)
from .session import ChatGPTSession, _identity_of
from .target_guard import GuardError


_GENERATION_PROBE_INSTALL_SCRIPT = r"""
() => {
  /* SHIRABE_GENERATION_PROBE_INSTALL_V1 */
  if (window.__shirabeGenerationProbeV1) {
    window.__shirabeGenerationProbeV1.userStopRequested = false;
    window.__shirabeGenerationProbeV1.stopClickCount = 0;
    return {installed: true, existing: true, reset: true};
  }
  const state = {userStopRequested: false, stopClickCount: 0};
  document.addEventListener('click', (event) => {
    let node = event.target;
    while (node && node !== document) {
      if (node instanceof Element) {
        const testid = String(node.getAttribute('data-testid') || '').toLowerCase();
        const aria = String(node.getAttribute('aria-label') || '').toLowerCase();
        const text = String(node.textContent || '').trim().toLowerCase();
        const stopLike = testid.includes('stop')
          || aria.includes('stop generating')
          || aria.includes('stop responding')
          || aria === 'stop'
          || text === 'stop'
          || text === '停止';
        if (stopLike) {
          state.userStopRequested = true;
          state.stopClickCount += 1;
          break;
        }
      }
      node = node.parentNode;
    }
  }, true);
  window.__shirabeGenerationProbeV1 = state;
  return {installed: true, existing: false};
}
"""

_GENERATION_OBSERVE_SCRIPT = r"""
() => {
  /* SHIRABE_GENERATION_OBSERVE_V1 */
  const visible = (el) => {
    if (!el) return false;
    try {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
    } catch (_) { return true; }
  };
  const stopCandidates = [
    document.querySelector('button[data-testid*="stop" i]'),
    ...[...document.querySelectorAll('button')].filter(b => {
      const aria = String(b.getAttribute('aria-label') || '').toLowerCase();
      const text = String(b.textContent || '').trim().toLowerCase();
      return aria.includes('stop generating') || aria.includes('stop responding') || aria === 'stop' || text === 'stop' || text === '停止';
    }),
  ].filter(Boolean);
  const stopVisible = [...new Set(stopCandidates)].some(visible);
  const streamCandidates = [
    ...document.querySelectorAll('[data-is-streaming="true"], .result-streaming')
  ];
  const streamingVisible = streamCandidates.some(visible);
  const probe = window.__shirabeGenerationProbeV1 || {};
  return {
    stopVisible,
    streamingVisible,
    userStopRequested: Boolean(probe.userStopRequested),
    stopClickCount: Number(probe.stopClickCount || 0),
  };
}
"""


@dataclass(frozen=True)
class _ObservedGenerationSignals:
    stop_visible: bool
    streaming_visible: bool
    user_stop_requested: bool
    stop_click_count: int


def observe_generation(session: ChatGPTSession) -> GenerationObservation:
    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        if not bind.ok:
            return GenerationObservation(
                state=GenerationState.UNKNOWN,
                stop_visible=False,
                user_stop_requested=False,
                streaming_visible=False,
                page=bind.page,
                evidence=bind.evidence,
            )
        bound = session._bound_page()
    if bound is None:
        return GenerationObservation(
            state=GenerationState.UNKNOWN,
            stop_visible=False,
            user_stop_requested=False,
            streaming_visible=False,
            page=None,
            evidence=WebDebugEvidence("generation.observe", ("bound-page=missing",)),
        )

    try:
        raw = bound.ops.evaluate(_GENERATION_OBSERVE_SCRIPT) or {}
        signals = _ObservedGenerationSignals(
            stop_visible=bool(raw.get("stopVisible")),
            streaming_visible=bool(raw.get("streamingVisible")),
            user_stop_requested=bool(raw.get("userStopRequested")),
            stop_click_count=max(0, int(raw.get("stopClickCount") or 0)),
        )
    except Exception as exc:
        return GenerationObservation(
            state=GenerationState.UNKNOWN,
            stop_visible=False,
            user_stop_requested=False,
            streaming_visible=False,
            page=_identity_of(bound.ops),
            evidence=WebDebugEvidence(
                "generation.observe",
                (f"observation-error={type(exc).__name__}",),
            ),
        )

    active = signals.stop_visible or signals.streaming_visible
    if signals.user_stop_requested and not active:
        state = GenerationState.USER_STOPPED
    elif active:
        state = GenerationState.RUNNING
    else:
        state = GenerationState.NOT_STARTED
    return GenerationObservation(
        state=state,
        stop_visible=signals.stop_visible,
        user_stop_requested=signals.user_stop_requested,
        streaming_visible=signals.streaming_visible,
        page=_identity_of(bound.ops),
        evidence=WebDebugEvidence(
            "generation.observe",
            (
                f"stop_visible={signals.stop_visible}",
                f"streaming_visible={signals.streaming_visible}",
                f"user_stop_requested={signals.user_stop_requested}",
                f"stop_click_count={signals.stop_click_count}",
            ),
        ),
    )


def submit_prompt_and_wait_for_response(
    session: ChatGPTSession,
    chat: ChatIdentity,
    prompt: str,
    *,
    attachments: tuple[dict[str, Any], ...] = (),
    submit_timeout_seconds: float = 10.0,
    generation_timeout_seconds: float = 900.0,
    poll_interval_seconds: float = 0.5,
    stable_samples_required: int = 3,
) -> PromptResponseResult:
    """Verified Prompt -> generation -> target assistant response operation.

    Completion requires a response that is new relative to a pre-submit baseline,
    evidence that generation actually started (generation UI or response change),
    generation inactivity, and stable semantic response identity for bounded
    consecutive samples.  USER_STOPPED is preserved as a separate terminal state.
    """

    if stable_samples_required < 1:
        raise ValueError("stable_samples_required must be >= 1")

    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        if not bind.ok:
            return _result_failure(
                GenerationState.UNKNOWN,
                chat,
                None,
                FailureKind.SESSION_BINDING_FAILURE,
                bind.reason or "ChatGPT page binding failed.",
                bind.page,
                bind.evidence,
            )
        bound = session._bound_page()
    if bound is None:
        return _result_failure(
            GenerationState.UNKNOWN,
            chat,
            None,
            FailureKind.SESSION_BINDING_FAILURE,
            "Bound page missing.",
            None,
            WebDebugEvidence("generation.wait", ("bound-page=missing",)),
        )

    baseline = observe_message_snapshot(session, expected_chat=chat)
    if isinstance(baseline, MessageObservationFailure):
        return _result_failure(
            GenerationState.UNKNOWN,
            chat,
            None,
            baseline.failure,
            baseline.reason,
            _identity_of(bound.ops),
            baseline.evidence,
        )

    attachment_evidence = None
    if attachments:
        attached = attach_files(
            session,
            chat,
            attachments,
            timeout_seconds=max(10.0, submit_timeout_seconds),
            poll_interval_seconds=min(0.1, max(0.01, poll_interval_seconds)),
        )
        if not attached.ok:
            return _result_failure(
                GenerationState.NOT_STARTED,
                chat,
                None,
                attached.failure or FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                attached.reason or "Prompt attachment postcondition failed.",
                attached.page,
                attached.evidence,
            )
        attachment_evidence = attached.evidence

    try:
        bound.ops.evaluate(_GENERATION_PROBE_INSTALL_SCRIPT)
    except Exception as exc:
        return _result_failure(
            GenerationState.UNKNOWN,
            chat,
            None,
            exc.failure_kind if isinstance(exc, GuardError) else FailureKind.UNKNOWN_WEB_STATE,
            exc.code if isinstance(exc, GuardError) else f"GENERATION_PROBE_FAILED:{type(exc).__name__}",
            _identity_of(bound.ops),
            WebDebugEvidence("generation.wait", ("probe-install=failed",)),
        )

    submission = submit_prompt(
        session,
        chat,
        prompt,
        timeout_seconds=submit_timeout_seconds,
        poll_interval_seconds=min(0.1, max(0.01, poll_interval_seconds)),
    )
    if not submission.ok:
        details = submission.evidence.details if submission.evidence is not None else {}
        activation_attempted = bool(details.get("activation_attempted"))
        if activation_attempted:
            try:
                session.require_reconciliation()
            except Exception:
                pass
        return _result_failure(
            GenerationState.UNKNOWN if activation_attempted else GenerationState.NOT_STARTED,
            submission.chat or chat,
            submission,
            FailureKind.RECONCILE_REQUIRED if activation_attempted else (submission.failure or FailureKind.POSTCONDITION_NOT_ESTABLISHED),
            submission.reason or "Prompt submission postcondition failed.",
            submission.page,
            submission.evidence,
        )

    active_chat = submission.chat or chat
    observations = [
        f"baseline.assistant_count={baseline.assistant_count}",
        f"baseline.user_count={baseline.user_count}",
        f"attachment_count={len(attachments)}",
        "submission=verified",
    ]
    if attachment_evidence is not None:
        observations.extend(f"attachment.{item}" for item in attachment_evidence.observations)
    deadline = time.monotonic() + max(0.0, generation_timeout_seconds)
    generation_seen = False
    response_seen = False
    stable_count = 0
    last_response_hash: str | None = None
    last_response = None
    last_snapshot: MessageSnapshot = baseline
    polls = 0

    def observation_uncertain(reason: str, *, error: Exception | None = None) -> PromptResponseResult:
        try:
            session.require_reconciliation()
        except Exception:
            pass
        safe_observations = observations + [f"terminal=RECONCILIATION_REQUIRED", f"reason={reason}"]
        if error is not None:
            safe_observations.append(f"error_type={type(error).__name__}")
        response = AssistantResponse(identity=last_response.identity, text=last_response.text) if last_response else None
        return PromptResponseResult(
            status=WebOperationStatus.FAILED, generation_state=GenerationState.UNKNOWN,
            chat=active_chat, submission=submission, response=response,
            page=_identity_of(bound.ops), failure=FailureKind.RECONCILE_REQUIRED,
            reason="RECONCILIATION_REQUIRED",
            evidence=WebDebugEvidence("generation.wait", tuple(safe_observations), {
                "polls": polls, "generation_seen": generation_seen,
                "stable_samples": stable_count, "send_state": "ESTABLISHED",
            }),
        )

    while True:
        polls += 1
        try:
            generation = observe_generation(session)
        except Exception:
            try:
                session.require_reconciliation()
            except Exception:
                pass
            raise
        if generation.state is GenerationState.UNKNOWN:
            # Unknown visibility is not evidence of generation inactivity. Do
            # not let stale response snapshots become a completed Work result.
            return observation_uncertain("generation-observation-unknown")
        try:
            snapshot = observe_message_snapshot(session, expected_chat=active_chat)
        except Exception:
            try:
                session.require_reconciliation()
            except Exception:
                pass
            raise
        if isinstance(snapshot, MessageObservationFailure):
            observations.append(f"poll.{polls}.messages={snapshot.failure.value}")
            return observation_uncertain("message-observation-unavailable")
        else:
            last_snapshot = snapshot
            if active_chat.conversation_id is None and snapshot.conversation_id:
                active_chat = ChatIdentity(
                    project_key=active_chat.project_key,
                    url=bound.ops.url,
                    conversation_id=snapshot.conversation_id,
                    provisional_id=None,
                    client_ref=active_chat.client_ref,
                )

        candidate = latest_new_assistant_response(baseline, snapshot)
        if candidate is not None and candidate.text.strip():
            response_seen = True
            last_response = candidate
            current_hash = candidate.identity.semantic_text_sha256
            if current_hash == last_response_hash:
                stable_count += 1
            else:
                stable_count = 1
                last_response_hash = current_hash
        else:
            stable_count = 0
            last_response_hash = None

        active_signal = generation.stop_visible or generation.streaming_visible
        if active_signal or candidate is not None:
            if not generation_seen:
                observations.append(f"generation=STARTED@poll.{polls}")
            elif active_signal:
                observations.append(f"generation=RUNNING@poll.{polls}")
            generation_seen = True

        observations.append(
            f"poll.{polls}=gen:{generation.state.value},active:{active_signal},response:{candidate is not None},stable:{stable_count}"
        )

        terminal_inactive = not generation.stop_visible and not generation.streaming_visible
        if generation.user_stop_requested and terminal_inactive and (not response_seen or stable_count >= stable_samples_required):
            response = AssistantResponse(identity=last_response.identity, text=last_response.text) if last_response else None
            observations.extend(("terminal=USER_STOPPED", f"polls={polls}"))
            return PromptResponseResult(
                status=WebOperationStatus.USER_STOPPED,
                generation_state=GenerationState.USER_STOPPED,
                chat=active_chat,
                submission=submission,
                response=response,
                page=_identity_of(bound.ops),
                failure=FailureKind.USER_STOPPED,
                reason="The ChatGPT Stop control was activated; partial assistant content became stable.",
                evidence=WebDebugEvidence(
                    "generation.wait",
                    tuple(observations),
                    {"polls": polls, "generation_seen": generation_seen, "stable_samples": stable_count},
                ),
            )

        if generation_seen and response_seen and terminal_inactive and stable_count >= stable_samples_required:
            assert last_response is not None
            observations.extend(("terminal=COMPLETED", f"polls={polls}"))
            return PromptResponseResult(
                status=WebOperationStatus.COMPLETED,
                generation_state=GenerationState.COMPLETED,
                chat=active_chat,
                submission=submission,
                response=AssistantResponse(identity=last_response.identity, text=last_response.text),
                page=_identity_of(bound.ops),
                evidence=WebDebugEvidence(
                    "generation.wait",
                    tuple(observations),
                    {"polls": polls, "generation_seen": generation_seen, "stable_samples": stable_count},
                ),
            )

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, poll_interval_seconds))

    observations.extend(
        (
            "terminal=TIMEOUT",
            f"polls={polls}",
            f"generation_seen={generation_seen}",
            f"response_seen={response_seen}",
        )
    )
    response = AssistantResponse(identity=last_response.identity, text=last_response.text) if last_response else None
    try:
        session.require_reconciliation()
    except Exception:
        pass
    return PromptResponseResult(
        status=WebOperationStatus.FAILED,
        generation_state=GenerationState.TIMEOUT,
        chat=active_chat,
        submission=submission,
        response=response,
        page=_identity_of(bound.ops),
        failure=FailureKind.TIMEOUT,
        reason="Generation/response completion postcondition was not established before timeout.",
        evidence=WebDebugEvidence(
            "generation.wait",
            tuple(observations),
            {"polls": polls, "generation_seen": generation_seen, "stable_samples": stable_count},
        ),
    )


def _result_failure(
    state: GenerationState,
    chat: ChatIdentity | None,
    submission: Any,
    failure: FailureKind,
    reason: str,
    page: Any,
    evidence: WebDebugEvidence | None,
) -> PromptResponseResult:
    return PromptResponseResult(
        status=WebOperationStatus.FAILED,
        generation_state=state,
        chat=chat,
        submission=submission,
        response=None,
        page=page,
        failure=failure,
        reason=reason,
        evidence=evidence,
    )
