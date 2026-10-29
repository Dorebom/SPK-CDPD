from __future__ import annotations

from typing import Any

from .models import (
    ComposerReadinessResult,
    ComposerAttachmentResult,
    AttachmentTransportObservationResult,
    ComposerState,
    FailureKind,
    WebDebugEvidence,
    WebOperationStatus,
)
from .session import ChatGPTSession, _identity_of
from .target_guard import GuardError


_COMPOSER_OBSERVE_SCRIPT = r"""
() => {
  /* SHIRABE_COMPOSER_OBSERVE_V1 */
  const rawCandidates = [
    document.querySelector('#prompt-textarea'),
    document.querySelector('textarea[data-testid*="prompt" i]'),
    document.querySelector('textarea'),
    document.querySelector('[contenteditable="true"][role="textbox"]'),
  ].filter(Boolean);
  const candidates = [...new Set(rawCandidates)];

  const visibleOf = (el) => {
    try {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && style.opacity !== '0'
        && rect.width > 0
        && rect.height > 0;
    } catch (_) { return true; }
  };
  const connectedOf = (el) => el.isConnected !== false && document.documentElement.contains(el);
  const hiddenOf = (el) => Boolean(el.hidden) || el.getAttribute('aria-hidden') === 'true';
  const editableOf = (el) => {
    const disabled = Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true';
    const readonly = Boolean(el.readOnly) || el.getAttribute('aria-readonly') === 'true';
    const contentEditable = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
    return !disabled && !readonly && (contentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT');
  };
  const textOf = (el) => {
    const text = ('value' in el ? el.value : el.textContent) || '';
    return String(text).replace(/\u200B/g, '').trim();
  };
  const semanticContainer = (el) => el.closest(
    'form, [data-testid*="composer" i], [data-testid*="prompt" i], [class*="composer" i]'
  );
  const explicitPromptIdentity = (el) => {
    const id = String(el.id || '').toLowerCase();
    const tid = String(el.getAttribute('data-testid') || '').toLowerCase();
    return id === 'prompt-textarea' || tid.includes('prompt');
  };
  const rank = (el) => {
    if (String(el.id || '').toLowerCase() === 'prompt-textarea') return 0;
    if (String(el.getAttribute('data-testid') || '').toLowerCase().includes('prompt')) return 1;
    if (el.getAttribute('contenteditable') === 'true' && el.getAttribute('role') === 'textbox') return 2;
    if (el.tagName === 'TEXTAREA') return 3;
    return 4;
  };

  const details = candidates.map((el, index) => {
    const container = semanticContainer(el);
    return {
      index,
      el,
      tag: String(el.tagName || '').toLowerCase(),
      id: String(el.id || ''),
      testId: String(el.getAttribute('data-testid') || ''),
      visible: visibleOf(el),
      editable: editableOf(el),
      connected: connectedOf(el),
      hidden: hiddenOf(el),
      explicitPrompt: explicitPromptIdentity(el),
      container,
      rank: rank(el),
    };
  });
  const active = details.filter(d => d.visible && d.editable && d.connected && !d.hidden);

  let logicalGroups = active.map(d => [d]);
  if (active.length > 1) {
    const allSameContainer = active.every(d => d.container && d.container === active[0].container);
    const hasExplicitPrompt = active.some(d => d.explicitPrompt);
    const allAncestorRelated = active.every((a, i) => active.every((b, j) => {
      if (i === j) return true;
      return a.el.contains(b.el) || b.el.contains(a.el);
    }));
    if ((allSameContainer && hasExplicitPrompt) || allAncestorRelated) {
      logicalGroups = [active];
    }
  }

  const logicalCount = logicalGroups.length;
  let selected = null;
  let selectionReason = 'no-active-candidate';
  if (logicalCount === 1) {
    selected = [...logicalGroups[0]].sort((a, b) => a.rank - b.rank || a.index - b.index)[0];
    selectionReason = logicalGroups[0].length > 1 ? 'logical-dedup' : 'single-active-candidate';
  } else if (logicalCount > 1) {
    selectionReason = 'multiple-logical-composers';
  }

  const el = selected ? selected.el : null;
  const text = el ? textOf(el) : '';
  return {
    present: Boolean(el),
    visible: Boolean(el && selected.visible),
    editable: Boolean(el && selected.editable),
    empty: Boolean(el && text.length === 0),
    textLength: text.length,
    candidateCount: candidates.length,
    activeCandidateCount: active.length,
    logicalCandidateCount: logicalCount,
    selectedCandidateIndex: selected ? selected.index : null,
    selectionReason,
    candidates: details.map(d => ({
      index: d.index,
      tag: d.tag,
      id: d.id,
      testId: d.testId,
      visible: d.visible,
      editable: d.editable,
      connected: d.connected,
      hidden: d.hidden,
      explicitPrompt: d.explicitPrompt,
      hasSemanticContainer: Boolean(d.container),
    })),
  };
}
"""


def observe_composer_readiness(
    session: ChatGPTSession,
    *,
    require_empty: bool = False,
) -> ComposerReadinessResult:
    """Observe the bound ChatGPT composer without exposing DOM selectors.

    `require_empty=True` is used by new-chat creation so automation never treats
    a stale/draft composer as a clean new-chat postcondition.
    """

    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        if not bind.ok:
            return ComposerReadinessResult(
                status=WebOperationStatus.FAILED,
                composer=None,
                page=bind.page,
                failure=bind.failure,
                reason=bind.reason,
                evidence=bind.evidence,
            )
        bound = session._bound_page()
    if bound is None:
        return _composer_failure(
            FailureKind.SESSION_BINDING_FAILURE,
            "Session reported bound but no internal page exists.",
            None,
            ["bound-page=missing"],
        )

    try:
        state, candidate_count = _observe_composer(bound.ops)
    except Exception as exc:
        return _composer_failure(
            FailureKind.UNKNOWN_WEB_STATE,
            f"Composer observation failed: {type(exc).__name__}: {exc}",
            _identity_of(bound.ops),
            ["composer-observation=failed"],
        )

    observations = _composer_observations(state, candidate_count)
    if candidate_count > 1:
        return _composer_failure(
            FailureKind.AMBIGUOUS_OBSERVATION,
            "Multiple composer candidates were observed; refusing to guess.",
            _identity_of(bound.ops),
            observations,
            state=state,
        )
    if not state.input_ready:
        return _composer_failure(
            FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            "Composer input surface is not ready.",
            _identity_of(bound.ops),
            observations,
            state=state,
        )
    if require_empty and not state.empty:
        return _composer_failure(
            FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            "Composer is input-ready but not empty.",
            _identity_of(bound.ops),
            observations,
            state=state,
        )

    observations.append("postcondition=composer-input-ready")
    if require_empty:
        observations.append("postcondition=composer-empty")
    return ComposerReadinessResult(
        status=WebOperationStatus.READY,
        composer=state,
        page=_identity_of(bound.ops),
        evidence=WebDebugEvidence(
            "composer.readiness",
            tuple(observations),
            {
                "require_empty": require_empty,
                "raw_candidate_count": int(getattr(candidate_count, "raw_count", int(candidate_count))),
                "active_candidate_count": int(getattr(candidate_count, "active_count", int(candidate_count))),
                "logical_candidate_count": int(candidate_count),
                "selection_reason": str(getattr(candidate_count, "selection_reason", "unknown")),
            },
        ),
    )


class _ComposerCandidateCount(int):
    """Logical Composer count with raw DOM diagnostics attached.

    The integer value is the number of independently actionable logical
    composers.  Existing callers can keep ordinary integer comparisons while
    evidence rendering can still distinguish raw DOM candidates from logical
    Composer identity.
    """

    def __new__(
        cls,
        logical_count: int,
        *,
        raw_count: int,
        active_count: int,
        selection_reason: str | None = None,
    ) -> "_ComposerCandidateCount":
        obj = int.__new__(cls, max(0, int(logical_count)))
        obj.raw_count = max(0, int(raw_count))
        obj.active_count = max(0, int(active_count))
        obj.selection_reason = selection_reason or "unknown"
        return obj


def _observe_composer(page: Any) -> tuple[ComposerState, int]:
    raw = page.evaluate(_COMPOSER_OBSERVE_SCRIPT) or {}
    state = ComposerState(
        present=bool(raw.get("present")),
        visible=bool(raw.get("visible")),
        editable=bool(raw.get("editable")),
        empty=bool(raw.get("empty")),
        text_length=max(0, int(raw.get("textLength") or 0)),
    )
    raw_count = max(0, int(raw.get("candidateCount") or (1 if state.present else 0)))
    active_count = max(0, int(raw.get("activeCandidateCount") or raw_count))
    logical_count = max(
        0,
        int(
            raw.get("logicalCandidateCount")
            if raw.get("logicalCandidateCount") is not None
            else raw_count
        ),
    )
    return state, _ComposerCandidateCount(
        logical_count,
        raw_count=raw_count,
        active_count=active_count,
        selection_reason=str(raw.get("selectionReason") or "legacy-observation"),
    )


def _composer_observations(state: ComposerState, candidate_count: int) -> list[str]:
    raw_count = int(getattr(candidate_count, "raw_count", int(candidate_count)))
    active_count = int(getattr(candidate_count, "active_count", int(candidate_count)))
    logical_count = int(candidate_count)
    selection_reason = str(getattr(candidate_count, "selection_reason", "unknown"))
    return [
        f"composer.candidate_count={raw_count}",
        f"composer.raw_candidate_count={raw_count}",
        f"composer.active_candidate_count={active_count}",
        f"composer.logical_candidate_count={logical_count}",
        f"composer.selection_reason={selection_reason}",
        f"composer.present={state.present}",
        f"composer.visible={state.visible}",
        f"composer.editable={state.editable}",
        f"composer.empty={state.empty}",
        f"composer.text_length={state.text_length}",
    ]


def _composer_failure(
    kind: FailureKind,
    reason: str,
    page: Any,
    observations: list[str],
    *,
    state: ComposerState | None = None,
) -> ComposerReadinessResult:
    return ComposerReadinessResult(
        status=WebOperationStatus.FAILED,
        composer=state,
        page=page,
        failure=kind,
        reason=reason,
        evidence=WebDebugEvidence("composer.readiness", tuple(observations)),
    )

_PROMPT_TEXT_CORRELATION_HELPERS_JS = r"""
  /* SHIRABE_PROMPT_CORRELATION_V3 SHIRABE_DOM_PROMPT_SERIALIZER_V1 */
  const normalizePromptText = (value) => String(value ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(/\u00A0/g, ' ')
    .replace(/[\u200B\u200C\u200D\uFEFF]/g, '')
    .split('\n')
    .map(line => line.replace(/[ \t]+$/g, ''))
    .join('\n')
    .trim();

  const PROMPT_BLOCK_TAGS = new Set([
    'P', 'DIV', 'LI', 'PRE', 'BLOCKQUOTE',
    'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
  ]);

  const serializePromptDomNode = (node) => {
    if (!node) return '';
    if (node.nodeType === 3) return String(node.nodeValue || '');
    if (node.nodeType !== 1) return '';

    const tag = String(node.tagName || '').toUpperCase();
    if (tag === 'BR') return '\n';

    let text = '';
    for (const child of Array.from(node.childNodes || [])) {
      text += serializePromptDomNode(child);
    }

    if (PROMPT_BLOCK_TAGS.has(tag)) {
      if (text.length === 0) return '\n';
      if (!text.endsWith('\n')) return text + '\n';
    }
    return text;
  };

  const serializeContentEditablePrompt = (el) => {
    let text = '';
    for (const child of Array.from((el && el.childNodes) || [])) {
      text += serializePromptDomNode(child);
    }
    return text.replace(/\n+$/g, '');
  };

  const extractPromptText = (el) => {
    let source = 'none';
    let serializationVersion = 'none';
    let rawText = '';
    if (el && 'value' in el) {
      source = 'value';
      serializationVersion = 'native-value-v1';
      rawText = el.value || '';
    } else if (el) {
      source = 'dom-serialized-contenteditable';
      serializationVersion = 'prompt-dom-v1';
      rawText = serializeContentEditablePrompt(el);
    }
    rawText = String(rawText || '');
    return {
      source,
      serializationVersion,
      rawTextLength: rawText.length,
      normalizedText: normalizePromptText(rawText),
    };
  };

  const promptFingerprint = (value) => {
    const text = String(value || '');
    let hash = 2166136261;
    for (let i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, '0');
  };

  const correlatePromptText = (el, prompt) => {
    const expectedText = normalizePromptText(prompt);
    const observed = extractPromptText(el);
    const actualText = observed.normalizedText;
    const commonLength = Math.min(expectedText.length, actualText.length);
    let firstDifferenceIndex = -1;
    for (let i = 0; i < commonLength; i += 1) {
      if (expectedText[i] !== actualText[i]) {
        firstDifferenceIndex = i;
        break;
      }
    }
    if (firstDifferenceIndex < 0 && expectedText.length !== actualText.length) {
      firstDifferenceIndex = commonLength;
    }
    return {
      matches: actualText === expectedText,
      normalizationVersion: 'prompt-text-v3',
      serializationVersion: observed.serializationVersion,
      source: observed.source,
      rawTextLength: observed.rawTextLength,
      expectedLength: expectedText.length,
      actualLength: actualText.length,
      expectedFingerprint: promptFingerprint(expectedText),
      actualFingerprint: promptFingerprint(actualText),
      firstDifferenceIndex,
      expectedCodePoint: firstDifferenceIndex >= 0 && firstDifferenceIndex < expectedText.length
        ? expectedText.codePointAt(firstDifferenceIndex)
        : null,
      actualCodePoint: firstDifferenceIndex >= 0 && firstDifferenceIndex < actualText.length
        ? actualText.codePointAt(firstDifferenceIndex)
        : null,
    };
  };
"""


_CONTENTEDITABLE_PROMPT_INPUT_HELPERS_JS = r"""
  /* SHIRABE_CONTENTEDITABLE_NATIVE_INPUT_V1 */
  const insertContentEditablePrompt = (el, prompt) => {
    const normalizedPrompt = String(prompt ?? '').replace(/\r\n?/g, '\n');
    if (!el || !(el.isContentEditable || el.getAttribute('contenteditable') === 'true')) {
      return {ok: false, reason: 'contenteditable-not-established'};
    }
    if (typeof document.execCommand !== 'function'
        || typeof document.createRange !== 'function'
        || typeof window.getSelection !== 'function') {
      return {ok: false, reason: 'native-editing-api-unavailable'};
    }

    const before = extractPromptText(el);
    if (normalizePromptText(before.normalizedText) !== '') {
      return {ok: false, reason: 'composer-not-empty-before-input'};
    }

    el.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);

    let beforeInputEventCount = 0;
    let inputEventCount = 0;
    const onBeforeInput = () => { beforeInputEventCount += 1; };
    const onInput = () => { inputEventCount += 1; };
    el.addEventListener('beforeinput', onBeforeInput);
    el.addEventListener('input', onInput);

    const lines = normalizedPrompt.split('\n');
    let insertTextCommandCount = 0;
    let insertParagraphCommandCount = 0;
    let commandFailure = null;
    try {
      for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index];
        if (line.length > 0) {
          const inserted = document.execCommand('insertText', false, line);
          insertTextCommandCount += 1;
          if (!inserted) {
            commandFailure = 'insertText';
            break;
          }
        }
        if (index < lines.length - 1) {
          const insertedBreak = document.execCommand('insertParagraph', false, null);
          insertParagraphCommandCount += 1;
          if (!insertedBreak) {
            commandFailure = 'insertParagraph';
            break;
          }
        }
      }
    } finally {
      el.removeEventListener('beforeinput', onBeforeInput);
      el.removeEventListener('input', onInput);
    }

    return {
      ok: commandFailure === null,
      reason: commandFailure === null ? 'native-editing-commands-dispatched' : `native-editing-command-failed:${commandFailure}`,
      method: 'contenteditable-exec-command-v1',
      selectionEstablished: selection.rangeCount === 1,
      lineBreakCount: Math.max(0, lines.length - 1),
      insertTextCommandCount,
      insertParagraphCommandCount,
      beforeInputEventCount,
      inputEventCount,
    };
  };
"""


_PROMPT_INPUT_SCRIPT = r"""
(prompt) => {
  /* SHIRABE_PROMPT_INPUT_V1 SHIRABE_PROMPT_SUBMIT_V1 */
""" + _PROMPT_TEXT_CORRELATION_HELPERS_JS + _CONTENTEDITABLE_PROMPT_INPUT_HELPERS_JS + r"""
  const rawCandidates = [
    document.querySelector('#prompt-textarea'),
    document.querySelector('textarea[data-testid*="prompt" i]'),
    document.querySelector('textarea'),
    document.querySelector('[contenteditable="true"][role="textbox"]'),
  ].filter(Boolean);
  const candidates = [...new Set(rawCandidates)];

  const visibleOf = (el) => {
    try {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && style.opacity !== '0'
        && rect.width > 0
        && rect.height > 0;
    } catch (_) { return true; }
  };
  const connectedOf = (el) => el.isConnected !== false && document.documentElement.contains(el);
  const hiddenOf = (el) => Boolean(el.hidden) || el.getAttribute('aria-hidden') === 'true';
  const editableOf = (el) => {
    const disabled = Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true';
    const readonly = Boolean(el.readOnly) || el.getAttribute('aria-readonly') === 'true';
    const contentEditable = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
    return !disabled && !readonly && (contentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT');
  };
  const semanticContainer = (el) => el.closest(
    'form, [data-testid*="composer" i], [data-testid*="prompt" i], [class*="composer" i]'
  );
  const explicitPromptIdentity = (el) => {
    const id = String(el.id || '').toLowerCase();
    const tid = String(el.getAttribute('data-testid') || '').toLowerCase();
    return id === 'prompt-textarea' || tid.includes('prompt');
  };
  const rank = (el) => {
    if (String(el.id || '').toLowerCase() === 'prompt-textarea') return 0;
    if (String(el.getAttribute('data-testid') || '').toLowerCase().includes('prompt')) return 1;
    if (el.getAttribute('contenteditable') === 'true' && el.getAttribute('role') === 'textbox') return 2;
    if (el.tagName === 'TEXTAREA') return 3;
    return 4;
  };

  const details = candidates.map((el, index) => ({
    index,
    el,
    visible: visibleOf(el),
    editable: editableOf(el),
    connected: connectedOf(el),
    hidden: hiddenOf(el),
    explicitPrompt: explicitPromptIdentity(el),
    container: semanticContainer(el),
    rank: rank(el),
  }));
  const active = details.filter(d => d.visible && d.editable && d.connected && !d.hidden);

  let logicalGroups = active.map(d => [d]);
  if (active.length > 1) {
    const allSameContainer = active.every(d => d.container && d.container === active[0].container);
    const hasExplicitPrompt = active.some(d => d.explicitPrompt);
    const allAncestorRelated = active.every((a, i) => active.every((b, j) => {
      if (i === j) return true;
      return a.el.contains(b.el) || b.el.contains(a.el);
    }));
    if ((allSameContainer && hasExplicitPrompt) || allAncestorRelated) logicalGroups = [active];
  }

  const logicalCount = logicalGroups.length;
  if (logicalCount !== 1) {
    return {
      ok: false,
      reason: 'composer-logical-candidate-count',
      candidateCount: candidates.length,
      activeCandidateCount: active.length,
      logicalCandidateCount: logicalCount,
    };
  }

  const group = logicalGroups[0];
  const selected = [...group].sort((a, b) => a.rank - b.rank || a.index - b.index)[0];
  const el = selected.el;
  const selectionReason = group.length > 1 ? 'logical-dedup' : 'single-active-candidate';
  el.focus();
  let inputMethod = 'unknown';
  let nativeInput = {
    ok: true,
    reason: 'native-value-setter',
    method: 'native-value-setter-v1',
    selectionEstablished: null,
    lineBreakCount: 0,
    insertTextCommandCount: 0,
    insertParagraphCommandCount: 0,
    beforeInputEventCount: 0,
    inputEventCount: 0,
  };
  if ('value' in el) {
    const proto = Object.getPrototypeOf(el);
    const descriptor = Object.getOwnPropertyDescriptor(proto, 'value')
      || (typeof HTMLTextAreaElement !== 'undefined' && Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value'))
      || (typeof HTMLInputElement !== 'undefined' && Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value'));
    if (descriptor && descriptor.set) descriptor.set.call(el, prompt);
    else el.value = prompt;
    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: prompt}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    inputMethod = 'native-value-setter-v1';
  } else {
    nativeInput = insertContentEditablePrompt(el, prompt);
    inputMethod = nativeInput.method || 'contenteditable-native-input-unavailable';
    if (!nativeInput.ok) {
      return {
        ok: false,
        reason: nativeInput.reason || 'contenteditable-native-input-failed',
        candidateCount: candidates.length,
        activeCandidateCount: active.length,
        logicalCandidateCount: logicalCount,
        selectionReason,
        inputMethod,
        inputSelectionEstablished: nativeInput.selectionEstablished,
        inputLineBreakCount: nativeInput.lineBreakCount,
        inputInsertTextCommandCount: nativeInput.insertTextCommandCount,
        inputInsertParagraphCommandCount: nativeInput.insertParagraphCommandCount,
        inputBeforeInputEventCount: nativeInput.beforeInputEventCount,
        inputEventCount: nativeInput.inputEventCount,
      };
    }
  }

  const correlation = correlatePromptText(el, prompt);
  return {
    ok: correlation.matches,
    reason: correlation.matches ? 'prompt-input-established' : 'prompt-input-not-established',
    candidateCount: candidates.length,
    activeCandidateCount: active.length,
    logicalCandidateCount: logicalCount,
    selectionReason,
    inputMethod,
    inputSelectionEstablished: nativeInput.selectionEstablished,
    inputLineBreakCount: nativeInput.lineBreakCount,
    inputInsertTextCommandCount: nativeInput.insertTextCommandCount,
    inputInsertParagraphCommandCount: nativeInput.insertParagraphCommandCount,
    inputBeforeInputEventCount: nativeInput.beforeInputEventCount,
    inputEventCount: nativeInput.inputEventCount,
    inputTextLength: correlation.actualLength,
    inputRawTextLength: correlation.rawTextLength,
    inputMatchesPrompt: correlation.matches,
    inputNormalizationVersion: correlation.normalizationVersion,
    inputSerializationVersion: correlation.serializationVersion,
    inputTextSource: correlation.source,
    inputExpectedFingerprint: correlation.expectedFingerprint,
    inputObservedFingerprint: correlation.actualFingerprint,
    inputFirstDifferenceIndex: correlation.firstDifferenceIndex,
    inputExpectedCodePoint: correlation.expectedCodePoint,
    inputObservedCodePoint: correlation.actualCodePoint,
  };
}
"""


_SEND_ACTION_DISPATCH_SCRIPT = r"""
(payload) => {
  /* SHIRABE_SEND_ACTION_SELECT_V2 SHIRABE_PROMPT_SUBMIT_V2 */
  const prompt = String((payload && payload.prompt) || '');
  const token = String((payload && payload.token) || '');
""" + _PROMPT_TEXT_CORRELATION_HELPERS_JS + r"""
  const rawCandidates = [
    document.querySelector('#prompt-textarea'),
    document.querySelector('textarea[data-testid*="prompt" i]'),
    document.querySelector('textarea'),
    document.querySelector('[contenteditable="true"][role="textbox"]'),
  ].filter(Boolean);
  const candidates = [...new Set(rawCandidates)];

  const visibleOf = (el) => {
    try {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && style.opacity !== '0'
        && rect.width > 0
        && rect.height > 0;
    } catch (_) { return true; }
  };
  const connectedOf = (el) => el.isConnected !== false && document.documentElement.contains(el);
  const hiddenOf = (el) => Boolean(el.hidden) || el.getAttribute('aria-hidden') === 'true';
  const editableOf = (el) => {
    const disabled = Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true';
    const readonly = Boolean(el.readOnly) || el.getAttribute('aria-readonly') === 'true';
    const contentEditable = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
    return !disabled && !readonly && (contentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT');
  };
  const semanticContainer = (el) => el.closest(
    'form, [data-testid*="composer" i], [data-testid*="prompt" i], [class*="composer" i]'
  );
  const explicitPromptIdentity = (el) => {
    const id = String(el.id || '').toLowerCase();
    const tid = String(el.getAttribute('data-testid') || '').toLowerCase();
    return id === 'prompt-textarea' || tid.includes('prompt');
  };
  const rank = (el) => {
    if (String(el.id || '').toLowerCase() === 'prompt-textarea') return 0;
    if (String(el.getAttribute('data-testid') || '').toLowerCase().includes('prompt')) return 1;
    if (el.getAttribute('contenteditable') === 'true' && el.getAttribute('role') === 'textbox') return 2;
    if (el.tagName === 'TEXTAREA') return 3;
    return 4;
  };

  const details = candidates.map((el, index) => ({
    index,
    el,
    visible: visibleOf(el),
    editable: editableOf(el),
    connected: connectedOf(el),
    hidden: hiddenOf(el),
    explicitPrompt: explicitPromptIdentity(el),
    container: semanticContainer(el),
    rank: rank(el),
  }));
  const active = details.filter(d => d.visible && d.editable && d.connected && !d.hidden);
  let logicalGroups = active.map(d => [d]);
  if (active.length > 1) {
    const allSameContainer = active.every(d => d.container && d.container === active[0].container);
    const hasExplicitPrompt = active.some(d => d.explicitPrompt);
    const allAncestorRelated = active.every((a, i) => active.every((b, j) => {
      if (i === j) return true;
      return a.el.contains(b.el) || b.el.contains(a.el);
    }));
    if ((allSameContainer && hasExplicitPrompt) || allAncestorRelated) logicalGroups = [active];
  }

  const logicalCount = logicalGroups.length;
  if (logicalCount !== 1) {
    return {
      ok: false,
      reason: 'composer-logical-candidate-count',
      candidateCount: candidates.length,
      activeCandidateCount: active.length,
      logicalCandidateCount: logicalCount,
      retryable: false,
    };
  }

  const group = logicalGroups[0];
  const selected = [...group].sort((a, b) => a.rank - b.rank || a.index - b.index)[0];
  const el = selected.el;
  const selectionReason = group.length > 1 ? 'logical-dedup' : 'single-active-candidate';
  const correlation = correlatePromptText(el, prompt);
  if (!correlation.matches) {
    return {
      ok: false,
      reason: 'prompt-input-not-established',
      candidateCount: candidates.length,
      activeCandidateCount: active.length,
      logicalCandidateCount: logicalCount,
      selectionReason,
      inputTextLength: correlation.actualLength,
      inputRawTextLength: correlation.rawTextLength,
      inputMatchesPrompt: false,
      inputNormalizationVersion: correlation.normalizationVersion,
      inputSerializationVersion: correlation.serializationVersion,
      inputTextSource: correlation.source,
      inputExpectedFingerprint: correlation.expectedFingerprint,
      inputObservedFingerprint: correlation.actualFingerprint,
      inputFirstDifferenceIndex: correlation.firstDifferenceIndex,
      inputExpectedCodePoint: correlation.expectedCodePoint,
      inputObservedCodePoint: correlation.actualCodePoint,
      retryable: false,
    };
  }

  const actionRoot = el.closest('form')
    || (el.parentElement && el.parentElement.closest('[data-testid*="composer" i], [class*="composer" i]'))
    || el.parentElement;
  if (!actionRoot) {
    return {
      ok: false,
      reason: 'send-action-root-not-established',
      candidateCount: candidates.length,
      activeCandidateCount: active.length,
      logicalCandidateCount: logicalCount,
      selectionReason,
      inputTextLength: correlation.actualLength,
      inputRawTextLength: correlation.rawTextLength,
      inputMatchesPrompt: true,
      inputNormalizationVersion: correlation.normalizationVersion,
      inputSerializationVersion: correlation.serializationVersion,
      inputTextSource: correlation.source,
      inputExpectedFingerprint: correlation.expectedFingerprint,
      inputObservedFingerprint: correlation.actualFingerprint,
      inputFirstDifferenceIndex: correlation.firstDifferenceIndex,
      inputExpectedCodePoint: correlation.expectedCodePoint,
      inputObservedCodePoint: correlation.actualCodePoint,
      sendRawCandidateCount: 0,
      sendActiveCandidateCount: 0,
      sendLogicalActionCount: 0,
      sendSelectionReason: 'no-composer-action-root',
      retryable: true,
    };
  }

  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const actionSemantic = (button) => {
    const testId = normalize(button.getAttribute('data-testid'));
    const aria = normalize(button.getAttribute('aria-label'));
    const title = normalize(button.getAttribute('title'));
    const text = normalize(button.textContent);
    const markers = [testId, aria, title, text].filter(Boolean);
    const joined = markers.join(' | ');
    const isStop = /(^|[^a-z])(stop|停止)([^a-z]|$)/.test(joined) || testId.includes('stop');
    const isVoice = /(voice|microphone|mic|音声|音声入力)/.test(joined);
    const isAttach = /(attach|attachment|upload|file|添付|ファイル)/.test(joined);
    const isSend = !isStop && !isVoice && !isAttach && (
      testId === 'send-button'
      || testId.includes('send-button')
      || testId === 'send'
      || aria === 'send'
      || aria === 'send message'
      || aria === '送信'
      || title === 'send'
      || title === 'send message'
      || title === '送信'
      || text === 'send'
      || text === '送信'
    );
    let kind = 'other';
    if (isStop) kind = 'stop';
    else if (isVoice) kind = 'voice';
    else if (isAttach) kind = 'attach';
    else if (isSend) kind = 'send';
    const signature = [kind, testId, aria, title, text].join('|');
    const rank = testId === 'send-button' ? 0
      : testId.includes('send-button') ? 1
      : aria === 'send' || aria === 'send message' || aria === '送信' ? 2
      : title === 'send' || title === 'send message' || title === '送信' ? 3
      : text === 'send' || text === '送信' ? 4
      : 9;
    return {kind, signature, rank, testId, aria, title, text};
  };

  const rawActionCandidates = [...new Set([
    ...actionRoot.querySelectorAll('button[data-testid="send-button"]'),
    ...actionRoot.querySelectorAll('button[data-testid*="send" i]'),
    ...actionRoot.querySelectorAll('button[aria-label]'),
    ...actionRoot.querySelectorAll('button[title]'),
    ...actionRoot.querySelectorAll('button'),
  ])];
  const actionDetails = rawActionCandidates.map((button, index) => {
    const semantic = actionSemantic(button);
    const enabled = !button.disabled && button.getAttribute('aria-disabled') !== 'true';
    return {
      index,
      button,
      visible: visibleOf(button),
      connected: connectedOf(button),
      hidden: hiddenOf(button),
      enabled,
      semantic,
    };
  });
  const activeSendActions = actionDetails.filter(d =>
    d.semantic.kind === 'send' && d.visible && d.connected && !d.hidden && d.enabled
  );

  const logicalSendGroups = [];
  for (const detail of activeSendActions) {
    let logicalGroup = logicalSendGroups.find(g => g[0].semantic.signature === detail.semantic.signature);
    if (!logicalGroup) {
      logicalGroup = [];
      logicalSendGroups.push(logicalGroup);
    }
    logicalGroup.push(detail);
  }

  const logicalSendCount = logicalSendGroups.length;
  let selectedSend = null;
  let sendSelectionReason = 'no-enabled-send-action';
  if (logicalSendCount === 1) {
    selectedSend = [...logicalSendGroups[0]].sort((a, b) =>
      a.semantic.rank - b.semantic.rank || a.index - b.index
    )[0];
    sendSelectionReason = logicalSendGroups[0].length > 1
      ? 'logical-send-dedup'
      : 'single-enabled-send-action';
  } else if (logicalSendCount > 1) {
    sendSelectionReason = 'multiple-logical-send-actions';
  }

  const common = {
    candidateCount: candidates.length,
    activeCandidateCount: active.length,
    logicalCandidateCount: logicalCount,
    selectionReason,
    inputTextLength: correlation.actualLength,
    inputRawTextLength: correlation.rawTextLength,
    inputMatchesPrompt: true,
    inputNormalizationVersion: correlation.normalizationVersion,
    inputSerializationVersion: correlation.serializationVersion,
    inputTextSource: correlation.source,
    inputExpectedFingerprint: correlation.expectedFingerprint,
    inputObservedFingerprint: correlation.actualFingerprint,
    inputFirstDifferenceIndex: correlation.firstDifferenceIndex,
    inputExpectedCodePoint: correlation.expectedCodePoint,
    inputObservedCodePoint: correlation.actualCodePoint,
    sendRawCandidateCount: rawActionCandidates.length,
    sendActiveCandidateCount: activeSendActions.length,
    sendLogicalActionCount: logicalSendCount,
    sendSelectionReason,
    sendActionKinds: actionDetails.map(d => d.semantic.kind),
  };

  if (!selectedSend) {
    return {
      ok: false,
      reason: logicalSendCount === 0 ? 'send-action-not-ready' : 'send-logical-action-count',
      retryable: logicalSendCount === 0,
      ...common,
    };
  }

  if (!token) return {ok:false,reason:'send-marker-token-missing',retryable:false,...common};
  selectedSend.button.setAttribute('data-shirabe-send-action', token);
  return {
    ok: true,
    reason: 'send-action-selected',
    retryable: false,
    sendMarkerAttribute: 'data-shirabe-send-action',
    sendMarkerToken: token,
    selectedSendSignature: selectedSend.semantic.signature,
    ...common,
  };
}
"""


_ATTACHMENT_ACTION_SELECT_SCRIPT = r"""
(payload) => {
  /* SHIRABE_ATTACHMENT_ACTION_SELECT_V3 */
  const token = String((payload && payload.token) || '');
  const composer = document.querySelector('#prompt-textarea')
    || document.querySelector('textarea[data-testid*="prompt" i]')
    || document.querySelector('[contenteditable="true"][role="textbox"]');
  if (!composer) {
    return {
      ok:false, reason:'composer-not-found', actionRootReason:'composer-not-found',
      actionRootCandidateCount:0, actionRootSelectedDepth:null, actionRootDetails:[],
      rawActionCount:0, activeActionCount:0, logicalActionCount:0,
    };
  }

  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    try {
      const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'
        && rect.width > 0 && rect.height > 0 && el.getAttribute('aria-hidden') !== 'true';
    } catch (_) { return true; }
  };
  const connected = (el) => el && el.isConnected !== false && document.documentElement.contains(el);
  const semantic = (el) => {
    const testId = normalize(el.getAttribute('data-testid'));
    const aria = normalize(el.getAttribute('aria-label'));
    const title = normalize(el.getAttribute('title'));
    const text = normalize(el.textContent);
    const joined = [testId, aria, title, text].filter(Boolean).join(' | ');
    const excluded = /(send|stop|voice|microphone|mic|送信|停止|音声)/.test(joined);
    const hasUpload = /(attach|attachment|upload|file|files|photo|image|添付|ファイル|アップロード|画像)/.test(joined);
    const hasAdd = /(composer[-_ ]?plus|plus|add|追加)/.test(joined);
    const isCandidate = !excluded && (hasUpload || hasAdd);
    const menuHint = /(composer[-_ ]?plus|plus)/.test(testId) || (!hasUpload && hasAdd);
    const kind = menuHint ? 'menu-opener' : 'attachment-action';
    const rank = testId.includes('composer-plus') ? 0
      : testId.includes('attach') || testId.includes('upload') ? 1
      : /attach|upload|file|添付|ファイル|アップロード/.test(aria) ? 2
      : /attach|upload|file|添付|ファイル|アップロード/.test(title) ? 3
      : hasUpload ? 4 : 8;
    return {isCandidate, kind, rank, testId, aria, title, text, signature:[kind,testId,aria,title,text].join('|')};
  };
  const uniqueInteractives = (root) => [...new Set([
    ...root.querySelectorAll('button'), ...root.querySelectorAll('[role="button"]'),
    ...root.querySelectorAll('[aria-haspopup]'), ...root.querySelectorAll('[data-testid]'),
  ])];
  const inspectRoot = (root, depth) => {
    const raw = uniqueInteractives(root);
    const details = raw.map((el,index) => {
      const sem=semantic(el); const enabled=!el.disabled && el.getAttribute('aria-disabled') !== 'true';
      const hidden=Boolean(el.hidden) || el.getAttribute('aria-hidden') === 'true';
      return {el,index,sem,enabled,visible:visible(el),connected:connected(el),hidden};
    });
    const activeInteractives=details.filter(d=>d.enabled&&d.visible&&d.connected&&!d.hidden);
    const active=activeInteractives.filter(d=>d.sem.isCandidate);
    const groups=[];
    for(const detail of active){ let group=groups.find(g=>g[0].sem.signature===detail.sem.signature); if(!group){group=[];groups.push(group);} group.push(detail); }
    return {root,depth,raw,activeInteractives,active,groups,diagnostic:{
      depth, tag:String(root.tagName||'').toLowerCase(), role:normalize(root.getAttribute&&root.getAttribute('role')),
      testId:normalize(root.getAttribute&&root.getAttribute('data-testid')),
      classHint:normalize(root.getAttribute&&root.getAttribute('class')).slice(0,240),
      rawInteractiveCount:raw.length, activeInteractiveCount:activeInteractives.length,
      attachmentCandidateCount:active.length, logicalAttachmentCount:groups.length,
    }};
  };

  const MAX_ANCESTOR_DEPTH=8; const inspected=[]; let cursor=composer.parentElement; let depth=1;
  while(cursor && depth<=MAX_ANCESTOR_DEPTH && cursor!==document.body && cursor!==document.documentElement){
    const state=inspectRoot(cursor,depth); inspected.push(state); if(state.groups.length>0) break;
    cursor=cursor.parentElement; depth+=1;
  }
  const rootDetails=inspected.map(s=>s.diagnostic);
  const rootsWithControls=inspected.filter(s=>s.activeInteractives.length>0);
  const selectedRoot=inspected.find(s=>s.groups.length>0)||null;
  if(!selectedRoot){
    const fallback=rootsWithControls.length?rootsWithControls[0]:(inspected.length?inspected[inspected.length-1]:null);
    const reason=rootsWithControls.length?'attachment-semantic-not-found':'composer-action-root-not-established';
    return {ok:false,reason,actionRootReason:reason,actionRootCandidateCount:rootsWithControls.length,
      actionRootSelectedDepth:null,actionRootDetails:rootDetails,rawActionCount:fallback?fallback.raw.length:0,
      activeActionCount:0,logicalActionCount:0,actionKinds:[],actionSignatures:[],activeActionDetails:[]};
  }
  const groups=selectedRoot.groups;
  if(groups.length!==1){
    return {ok:false,reason:'attachment-action-ambiguous',actionRootReason:'minimal-semantic-neighborhood',
      actionRootCandidateCount:rootsWithControls.length,actionRootSelectedDepth:selectedRoot.depth,
      actionRootDetails:rootDetails,rawActionCount:selectedRoot.raw.length,activeActionCount:selectedRoot.active.length,
      logicalActionCount:groups.length,actionKinds:selectedRoot.active.map(d=>d.sem.kind),
      actionSignatures:selectedRoot.active.map(d=>d.sem.signature),activeActionDetails:selectedRoot.active.map(d=>({kind:d.sem.kind,rank:d.sem.rank,testId:d.sem.testId,aria:d.sem.aria,title:d.sem.title,text:d.sem.text,signature:d.sem.signature,enabled:d.enabled,visible:d.visible,connected:d.connected,hidden:d.hidden}))};
  }
  const selected=[...groups[0]].sort((a,b)=>a.sem.rank-b.sem.rank||a.index-b.index)[0];
  selected.el.setAttribute('data-shirabe-attachment-action',token);

  let baselineVisibleInteractiveCount=0;
  {
    const deltaSelector='button, [role="button"], [role="menuitem"], [role="option"], a[href], [aria-label], [data-testid], [tabindex]:not([tabindex="-1"])';
    const baseline=[...new Set([...document.querySelectorAll(deltaSelector)])].filter(el=>visible(el)&&connected(el));
    baselineVisibleInteractiveCount=baseline.length;
    const state={baselineElements:baseline,startedAt:performance.now(),mutationAddedCount:0,mutationAttributeChangeCount:0,mutationSamples:[]};
    const observer=new MutationObserver(records=>{
      for(const record of records){
        if(record.type==='childList') state.mutationAddedCount += record.addedNodes ? record.addedNodes.length : 0;
        if(record.type==='attributes') state.mutationAttributeChangeCount += 1;
        if(state.mutationSamples.length<24){
          const target=record.target && record.target.nodeType===1 ? record.target : null;
          state.mutationSamples.push({type:record.type,attributeName:record.attributeName||'',tag:target?String(target.tagName||'').toLowerCase():'',role:target?normalize(target.getAttribute('role')):'',testId:target?normalize(target.getAttribute('data-testid')):''});
        }
      }
    });
    observer.observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['hidden','style','class','aria-hidden','aria-expanded','data-state','open']});
    state.observer=observer;
    window.__shirabeAttachmentDelta = window.__shirabeAttachmentDelta || {};
    window.__shirabeAttachmentDelta[token]=state;
  }

  return {ok:true,reason:groups[0].length>1?'logical-attachment-action-dedup':'single-attachment-action',
    actionRootReason:'minimal-semantic-neighborhood',actionRootCandidateCount:rootsWithControls.length,
    actionRootSelectedDepth:selectedRoot.depth,actionRootDetails:rootDetails,rawActionCount:selectedRoot.raw.length,
    activeActionCount:selectedRoot.active.length,logicalActionCount:1,selectedKind:selected.sem.kind,
    selectedSignature:selected.sem.signature,selectedRank:selected.sem.rank,
    triggerAriaControls:String(selected.el.getAttribute('aria-controls')||''),triggerAriaOwns:String(selected.el.getAttribute('aria-owns')||''),
    triggerAriaExpanded:String(selected.el.getAttribute('aria-expanded')||''),
    triggerRect:(()=>{try{const r=selected.el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,left:r.left,top:r.top,right:r.right,bottom:r.bottom};}catch(_){return null;}})(),
    causalDeltaToken:token,baselineVisibleInteractiveCount,
    activeActionDetails:selectedRoot.active.map(d=>({kind:d.sem.kind,rank:d.sem.rank,testId:d.sem.testId,aria:d.sem.aria,title:d.sem.title,text:d.sem.text,signature:d.sem.signature,enabled:d.enabled,visible:d.visible,connected:d.connected,hidden:d.hidden}))};
}
"""


_UPLOAD_MENU_ACTION_SELECT_SCRIPT = r"""
async (payload) => {
  /* SHIRABE_UPLOAD_MENU_ACTION_SELECT_V4 */
  const token=String((payload&&payload.token)||'');
  const deltaToken=String((payload&&payload.causalDeltaToken)||'');
  const triggerRect=(payload&&payload.triggerRect&&typeof payload.triggerRect==='object')?payload.triggerRect:null;
  const observationMs=Math.min(1800,Math.max(250,Number((payload&&payload.deltaObservationMs)||900)));
  const pollMs=50;
  const normalize=(value)=>String(value||'').replace(/\\s+/g,' ').trim().toLowerCase();
  const visible=(el)=>{try{const s=getComputedStyle(el);const r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&r.width>0&&r.height>0&&el.getAttribute('aria-hidden')!=='true';}catch(_){return true;}};
  const connected=(el)=>el&&el.isConnected!==false&&document.documentElement.contains(el);
  const rectOf=(el)=>{try{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,left:r.left,top:r.top,right:r.right,bottom:r.bottom};}catch(_){return null;}};
  const domPath=(el)=>{const parts=[];let cursor=el;let depth=0;while(cursor&&cursor.nodeType===1&&cursor!==document.body&&depth<8){const parent=cursor.parentElement;let nth=1;if(parent){const siblings=[...parent.children].filter(x=>x.tagName===cursor.tagName);nth=Math.max(1,siblings.indexOf(cursor)+1);}parts.push(String(cursor.tagName||'').toLowerCase()+':'+nth);cursor=parent;depth+=1;}return parts.reverse().join('/');};
  const nodeSignature=(el)=>[String(el.id||''),normalize(el.getAttribute('role')),normalize(el.getAttribute('data-testid')),normalize(el.getAttribute('aria-label')),normalize(el.textContent).slice(0,160),domPath(el)].join('|');
  const selector='button, [role="button"], [role="menuitem"], [role="option"], a[href], [aria-label], [data-testid], [tabindex]:not([tabindex="-1"])';
  const snapshot=()=>[...new Set([...document.querySelectorAll(selector)])].filter(el=>visible(el)&&connected(el));
  const semantic=(el)=>{
    const testId=normalize(el.getAttribute('data-testid'));const aria=normalize(el.getAttribute('aria-label'));const title=normalize(el.getAttribute('title'));const text=normalize(el.textContent);
    const joined=[testId,aria,title,text].filter(Boolean).join(' | ');
    const cloud=/(library|drive|onedrive|dropbox|sharepoint|connect|ライブラリ|クラウド)/.test(joined);
    const strongLocal=/(upload files?|upload from (computer|device)|choose files?|select files?|browse files?|add (photos?|images?)?\\s*(?:&|and)?\\s*files?|ファイルをアップロード|ファイルを選択|ファイルを追加|写真.*ファイル.*追加)/.test(joined);
    const verb=/(upload|choose|select|browse|add|アップロード|選択|追加)/.test(joined);
    const object=/(file|files|photo|photos|image|images|computer|device|local|ファイル|写真|画像|コンピュータ|端末|デバイス)/.test(joined);
    const explicitLocal=/(computer|device|local|コンピュータ|端末|デバイス)/.test(joined);
    const isUpload=strongLocal||(verb&&object&&(!cloud||explicitLocal));
    const rank=/(upload from (computer|device)|computer|device|local|コンピュータ|端末|デバイス)/.test(joined)?0:/(upload files?|ファイルをアップロード)/.test(joined)?1:strongLocal?2:9;
    return {isUpload,rank,testId,aria,title,text,signature:[testId,aria,title,text].join('|')};
  };
  const state=(window.__shirabeAttachmentDelta&&window.__shirabeAttachmentDelta[deltaToken])||null;
  const finish=(result)=>{
    if(state&&state.observer){try{state.observer.disconnect();}catch(_){}}
    if(window.__shirabeAttachmentDelta&&deltaToken){try{delete window.__shirabeAttachmentDelta[deltaToken];}catch(_){}}
    return result;
  };
  if(!state){
    return finish({ok:false,reason:'causal-interactive-baseline-missing',popupDiscoveryMode:'causal-interactive-delta',
      causalObservationDurationMs:0,causalPollCount:0,mutationAddedCount:0,mutationAttributeChangeCount:0,
      visibleInteractiveBeforeCount:0,visibleInteractiveAfterCount:0,newlyVisibleInteractiveCount:0,newlyVisibleInteractiveDetails:[],
      popupNeighborhoodReason:'causal-interactive-baseline-missing',popupNeighborhoodSelectedDepth:null,popupNeighborhoodSignature:null,
      popupRootCandidateCount:0,menuRootCount:0,rawMenuActionCount:0,activeMenuActionCount:0,logicalMenuActionCount:0});
  }
  const baseline=new Set(state.baselineElements||[]); const started=performance.now(); let pollCount=0; let after=[]; let delta=[]; let local=[];
  const nearTrigger=(el)=>{
    if(!triggerRect) return true; const r=el.getBoundingClientRect(); const cx=Number(triggerRect.left??triggerRect.x??0)+Number(triggerRect.width||0)/2; const cy=Number(triggerRect.top??triggerRect.y??0)+Number(triggerRect.height||0)/2; const rx=r.left+r.width/2; const ry=r.top+r.height/2; return Math.hypot(rx-cx,ry-cy)<=900;
  };
  while(performance.now()-started<observationMs){
    pollCount+=1; after=snapshot(); delta=after.filter(el=>!baseline.has(el));
    const near=delta.filter(nearTrigger); local=(near.length?near:delta).map((el,index)=>({el,index,sem:semantic(el)})).filter(d=>d.sem.isUpload);
    if(local.length>0){ await new Promise(resolve=>setTimeout(resolve,80)); after=snapshot(); delta=after.filter(el=>!baseline.has(el)); break; }
    await new Promise(resolve=>setTimeout(resolve,pollMs));
  }
  const nearDelta=(delta.filter(nearTrigger).length?delta.filter(nearTrigger):delta);
  const deltaDetails=nearDelta.slice(0,80).map(el=>{const sem=semantic(el);return {tag:String(el.tagName||'').toLowerCase(),role:normalize(el.getAttribute('role')),testId:normalize(el.getAttribute('data-testid')),aria:normalize(el.getAttribute('aria-label')),text:normalize(el.textContent).slice(0,240),signature:nodeSignature(el),rect:rectOf(el),localUpload:sem.isUpload,localUploadRank:sem.rank};});
  const localDetails=nearDelta.map((el,index)=>({el,index,sem:semantic(el)})).filter(d=>d.sem.isUpload);
  const byRank=[...localDetails].sort((a,b)=>a.sem.rank-b.sem.rank||a.index-b.index);
  const bestRank=byRank.length?byRank[0].sem.rank:null;
  const best=bestRank===null?[]:byRank.filter(d=>d.sem.rank===bestRank);
  const logical=[]; for(const detail of best){let group=logical.find(g=>g[0].sem.signature===detail.sem.signature);if(!group){group=[];logical.push(group);}group.push(detail);}

  let selected=logical.length===1?[...logical[0]].sort((a,b)=>a.index-b.index)[0]:null;
  let neighborhood=null; let neighborhoodDepth=null; let neighborhoodReason=null;
  if(selected){
    let cursor=selected.el.parentElement; let depth=1;
    while(cursor&&cursor!==document.body&&cursor!==document.documentElement&&depth<=8){
      const contained=nearDelta.filter(el=>cursor.contains(el));
      if(contained.length>=2){neighborhood=cursor;neighborhoodDepth=depth;neighborhoodReason='minimal-causal-interactive-neighborhood';break;}
      cursor=cursor.parentElement;depth+=1;
    }
    if(!neighborhood){neighborhood=selected.el.parentElement||selected.el;neighborhoodDepth=1;neighborhoodReason='single-action-parent-neighborhood';}
  } else if(nearDelta.length){
    let cursor=nearDelta[0].parentElement;let depth=1;
    while(cursor&&cursor!==document.body&&cursor!==document.documentElement&&depth<=8){if(nearDelta.filter(el=>cursor.contains(el)).length===nearDelta.length){neighborhood=cursor;neighborhoodDepth=depth;neighborhoodReason='delta-common-neighborhood';break;}cursor=cursor.parentElement;depth+=1;}
  }
  const common={
    popupDiscoveryMode:'causal-interactive-delta',popupBeforeRootCount:0,popupAfterRootCount:neighborhood?1:0,popupNewRootCount:neighborhood?1:0,
    popupRootCandidateCount:neighborhood?1:0,popupSelectedSignature:neighborhood?nodeSignature(neighborhood):null,
    popupRootDetails:neighborhood?[{signature:nodeSignature(neighborhood),role:normalize(neighborhood.getAttribute('role')),testId:normalize(neighborhood.getAttribute('data-testid')),rect:rectOf(neighborhood),newlyVisible:true}]:[],
    causalObservationDurationMs:Math.round(performance.now()-started),causalPollCount:pollCount,
    mutationAddedCount:Number(state.mutationAddedCount||0),mutationAttributeChangeCount:Number(state.mutationAttributeChangeCount||0),mutationSamples:state.mutationSamples||[],
    visibleInteractiveBeforeCount:baseline.size,visibleInteractiveAfterCount:after.length,newlyVisibleInteractiveCount:nearDelta.length,newlyVisibleInteractiveDetails:deltaDetails,
    popupNeighborhoodReason:neighborhoodReason,popupNeighborhoodSelectedDepth:neighborhoodDepth,popupNeighborhoodSignature:neighborhood?nodeSignature(neighborhood):null,
    menuRootCount:neighborhood?1:0,rawMenuActionCount:nearDelta.length,activeMenuActionCount:localDetails.length,logicalMenuActionCount:logical.length,
    menuActionSignatures:localDetails.map(d=>d.sem.signature),activeMenuActionDetails:localDetails.map(d=>({rank:d.sem.rank,testId:d.sem.testId,aria:d.sem.aria,title:d.sem.title,text:d.sem.text,signature:d.sem.signature,enabled:true,visible:true,connected:true,hidden:false})),
  };
  if(nearDelta.length===0) return finish({ok:false,reason:'causal-interactive-delta-not-observed',...common});
  if(logical.length===0) return finish({ok:false,reason:'local-upload-action-not-found',...common});
  if(logical.length!==1) return finish({ok:false,reason:'local-upload-action-ambiguous',...common});

  // The semantic node can be an inner text/layout div while the trusted click
  // handler lives on an enclosing Radix/menu row. Resolve activation authority
  // separately from semantic identity and never escape the reconstructed causal
  // neighborhood.
  const activationCandidates=[]; const seenActivation=new Set();
  const activationDetail=(el,source,distance)=>{
    if(!el||!connected(el)||!visible(el)||seenActivation.has(el)) return;
    if(neighborhood && !neighborhood.contains(el) && el!==neighborhood) return;
    seenActivation.add(el);
    const tag=String(el.tagName||'').toLowerCase(); const role=normalize(el.getAttribute('role'));
    const testId=normalize(el.getAttribute('data-testid')); const aria=normalize(el.getAttribute('aria-label'));
    const tabindexRaw=el.getAttribute('tabindex'); const tabindex=tabindexRaw===null?null:Number(tabindexRaw);
    const radix=el.hasAttribute('data-radix-collection-item'); let cursor=''; let pointerEvents='';
    try{const style=getComputedStyle(el);cursor=normalize(style.cursor);pointerEvents=normalize(style.pointerEvents);}catch(_){}
    const intrinsic=(tag==='button'||tag==='label'||tag==='input'||tag==='a'||role==='button'||role==='menuitem'||role==='option');
    let rank=8;
    if(intrinsic) rank=0;
    else if(radix) rank=1;
    else if(tabindex!==null&&Number.isFinite(tabindex)) rank=2;
    else if(cursor==='pointer') rank=3;
    else if(el===selected.el) rank=7;
    activationCandidates.push({el,source,distance,rank,tag,role,testId,aria,tabindex,radix,cursor,pointerEvents,signature:nodeSignature(el),rect:rectOf(el)});
  };
  // Descendant intrinsic actions/labels are strongest when present.
  for(const el of selected.el.querySelectorAll('button, label, [role="button"], [role="menuitem"], [role="option"], [data-radix-collection-item], [tabindex]')) activationDetail(el,'descendant',1);
  activationDetail(selected.el,'semantic-self',0);
  let ancestor=selected.el.parentElement; let ancestorDistance=1;
  while(ancestor&&ancestor!==document.body&&ancestor!==document.documentElement&&ancestorDistance<=8){
    activationDetail(ancestor,'ancestor',ancestorDistance);
    if(ancestor===neighborhood) break;
    ancestor=ancestor.parentElement; ancestorDistance+=1;
  }
  // Capture hit-test lineage as an additional signal without making page-global
  // elements eligible: only nodes inside the same causal neighborhood survive.
  const sr=rectOf(selected.el);
  if(sr){
    const hx=sr.left+sr.width/2, hy=sr.top+sr.height/2;
    try{for(const el of document.elementsFromPoint(hx,hy)) activationDetail(el,'hit-test',0);}catch(_){}
  }
  const viable=activationCandidates.filter(c=>c.pointerEvents!=='none');
  viable.sort((a,b)=>a.rank-b.rank || (a.source==='ancestor'&&b.source==='ancestor'?a.distance-b.distance:0) || a.distance-b.distance);
  const activation=viable.length?viable[0]:null;
  const bestActivationRank=activation?activation.rank:null;
  const topActivation=bestActivationRank===null?[]:viable.filter(c=>c.rank===bestActivationRank);
  const activationDetails=viable.slice(0,24).map(c=>({source:c.source,distance:c.distance,rank:c.rank,tag:c.tag,role:c.role,testId:c.testId,aria:c.aria,tabindex:c.tabindex,dataRadixCollectionItem:c.radix,cursor:c.cursor,pointerEvents:c.pointerEvents,signature:c.signature,rect:c.rect}));
  if(!activation) return finish({ok:false,reason:'local-upload-activation-target-not-found',selectedSignature:selected.sem.signature,selectedRank:selected.sem.rank,activationTargetReason:'not-found',activationTargetCandidateCount:0,activationTargetDetails:[],...common,logicalMenuActionCount:1});
  // Multiple top-ranked candidates in separate DOM branches are unsafe. Ancestor/
  // self/hit-test aliases of the same lineage are deterministic and deduplicated.
  const unrelatedTop=topActivation.filter(c=>!(c.el.contains(activation.el)||activation.el.contains(c.el)));
  if(unrelatedTop.length) return finish({ok:false,reason:'local-upload-activation-target-ambiguous',selectedSignature:selected.sem.signature,selectedRank:selected.sem.rank,activationTargetReason:'ambiguous-best-rank',activationTargetCandidateCount:topActivation.length,activationTargetDetails:activationDetails,...common,logicalMenuActionCount:1});
  activation.el.setAttribute('data-shirabe-upload-menu-action',token);
  return finish({ok:true,reason:logical[0].length>1?'logical-local-upload-action-dedup':'single-local-upload-action',selectedSignature:selected.sem.signature,selectedRank:selected.sem.rank,
    activationTargetReason:'ranked-causal-row-target',activationTargetCandidateCount:viable.length,activationTargetSelectedRank:activation.rank,activationTargetSelectedSource:activation.source,
    activationTargetSelectedTag:activation.tag,activationTargetSelectedRole:activation.role,activationTargetSelectedSignature:activation.signature,activationTargetDetails:activationDetails,
    ...common,logicalMenuActionCount:1});
}
"""


_ATTACHMENT_OBSERVE_SCRIPT = r"""
(arg) => {
  /* SHIRABE_ATTACHMENT_OBSERVE_V2 */
  const expectedNames = Array.isArray(arg) ? arg.map(String) : ((arg && arg.expectedNames) || []).map(String);
  const baselineSignatures = new Set(Array.isArray(arg && arg.baselineSignatures) ? arg.baselineSignatures.map(String) : []);
  const normalize = (value) => String(value || '').replace(/\u200B/g, '').replace(/\s+/g, ' ').trim();
  const lower = (value) => normalize(value).toLowerCase();
  const visibleOf = (el) => {
    try {
      if (!el || el.isConnected === false || !document.documentElement.contains(el)) return false;
      if (el.hidden || el.getAttribute('aria-hidden') === 'true') return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'
        && rect.width > 0 && rect.height > 0;
    } catch (_) { return false; }
  };
  const rectOf = (el) => {
    try {
      const r = el.getBoundingClientRect();
      return {x:r.x,y:r.y,left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};
    } catch (_) { return null; }
  };
  const composer = document.querySelector('#prompt-textarea')
    || document.querySelector('textarea[data-testid*="prompt" i]')
    || document.querySelector('[contenteditable="true"][role="textbox"]');
  if (!composer) return {ok:false, reason:'composer-not-found', observed:[], expectedCount:expectedNames.length, observedCount:0, candidates:[]};

  let root = composer.closest('form');
  let rootReason = root ? 'closest-form' : null;
  if (!root) {
    let node = composer;
    for (let depth=1; node && depth<=8; depth++, node=node.parentElement) {
      const testId = lower(node.getAttribute && node.getAttribute('data-testid'));
      const cls = lower(node.className);
      if (testId.includes('composer') || cls.includes('composer')) {
        root = node;
        rootReason = `bounded-composer-ancestor:${depth}`;
        break;
      }
    }
  }
  if (!root) {
    root = composer.parentElement || composer;
    rootReason = 'composer-parent-fallback';
  }

  const rootRect = rectOf(root) || rectOf(composer);
  const sources = [];
  const seen = new Set();
  const add = (el, scope) => {
    if (!el || seen.has(el) || !visibleOf(el)) return;
    seen.add(el);
    sources.push({el, scope});
  };
  add(root, 'root');
  try { root.querySelectorAll('*').forEach(el => add(el, 'root-descendant')); } catch (_) {}

  // Attachment cards may be rendered as a sibling/portal near the Composer rather
  // than as text inside the editor root.  Include only a bounded geometric
  // neighborhood so sidebar/history content cannot satisfy the postcondition.
  if (rootRect) {
    const nearSelector = '[aria-label],[title],[data-testid],[data-filename],[data-file-name],[data-name],[data-attachment-name],[role],button,a,div,span';
    try {
      document.querySelectorAll(nearSelector).forEach(el => {
        if (seen.has(el) || !visibleOf(el)) return;
        const r = rectOf(el); if (!r) return;
        const horizontal = r.right >= rootRect.left - 48 && r.left <= rootRect.right + 48;
        const vertical = r.bottom >= rootRect.top - 240 && r.top <= rootRect.bottom + 180;
        if (horizontal && vertical) add(el, 'composer-geometry');
      });
    } catch (_) {}
  }

  const dynamicData = (el) => {
    const out = {};
    try {
      for (const attr of [...el.attributes]) {
        const name = String(attr.name || '');
        if (!name.startsWith('data-')) continue;
        if (!/(file|attach|upload|name|asset|blob)/i.test(name)) continue;
        out[name] = normalize(attr.value).slice(0, 240);
      }
    } catch (_) {}
    return out;
  };
  const pathOf = (el) => {
    const parts = [];
    let node = el;
    for (let depth=0; node && node !== root && depth<8; depth++, node=node.parentElement) {
      const tag = String(node.tagName || '').toLowerCase();
      if (!tag) break;
      let index = 1;
      let sib = node.previousElementSibling;
      while (sib) { if (sib.tagName === node.tagName) index++; sib = sib.previousElementSibling; }
      parts.push(`${tag}:${index}`);
    }
    return parts.reverse().join('/');
  };
  const describe = ({el, scope}) => {
    const tag = String(el.tagName || '').toLowerCase();
    const role = normalize(el.getAttribute('role'));
    const testId = normalize(el.getAttribute('data-testid'));
    const aria = normalize(el.getAttribute('aria-label'));
    const title = normalize(el.getAttribute('title'));
    const text = normalize(el.textContent).slice(0, 360);
    const data = dynamicData(el);
    const classHint = normalize(typeof el.className === 'string' ? el.className : '').slice(0, 220);
    const values = [text, aria, title, testId, ...Object.values(data)];
    const matches = [];
    expectedNames.forEach(name => {
      const expected = String(name);
      const sourceNames = ['text','aria','title','testId', ...Object.keys(data)];
      values.forEach((value, idx) => {
        if (value && String(value).includes(expected)) matches.push({name:expected, source:sourceNames[idx] || 'data'});
      });
    });
    const semanticHay = lower([aria,title,testId,classHint,text,...Object.entries(data).flat()].join(' '));
    const attachmentLike = /(attach|attachment|upload|file|paperclip|remove file|remove attachment|添付|ファイル|アップロード)/i.test(semanticHay);
    const path = pathOf(el);
    const signature = [tag,role,testId,aria,title,text.slice(0,160),path].join('|');
    return {tag,role,testId,aria,title,text,data,classHint,scope,path,signature,rect:rectOf(el),attachmentLike,matches};
  };

  const all = sources.slice(0, 1200).map(describe);
  const candidates = all.filter(d => d.attachmentLike || d.matches.length).slice(0, 96);
  const matchedNames = expectedNames.filter(name => candidates.some(c => c.matches.some(m => m.name === name)));
  const matchDetails = {};
  expectedNames.forEach(name => {
    matchDetails[name] = candidates.filter(c => c.matches.some(m => m.name === name)).slice(0, 12);
  });
  const newCandidates = candidates.filter(c => !baselineSignatures.has(c.signature));
  const newAttachmentCandidates = newCandidates.filter(c => c.attachmentLike);
  const exactIdentity = matchedNames.length === expectedNames.length && expectedNames.length > 0;
  return {
    ok: exactIdentity,
    reason: exactIdentity ? 'all-attachment-identities-observed' : 'attachment-entity-identity-pending',
    observed: matchedNames,
    expectedCount: expectedNames.length,
    observedCount: matchedNames.length,
    rootReason,
    rootTag: String(root.tagName || '').toLowerCase(),
    rootTestId: normalize(root.getAttribute && root.getAttribute('data-testid')),
    rootClassHint: normalize(typeof root.className === 'string' ? root.className : '').slice(0,220),
    rootRect,
    scannedVisibleCount: all.length,
    candidateCount: candidates.length,
    candidates,
    candidateSignatures: candidates.map(c => c.signature),
    matchDetails,
    newCandidateCount: newCandidates.length,
    newCandidates: newCandidates.slice(0,48),
    newAttachmentCandidateCount: newAttachmentCandidates.length,
    newAttachmentCandidates: newAttachmentCandidates.slice(0,48),
  };
}
"""



def observe_attachment_transport(
    session: ChatGPTSession,
    *,
    timeout_seconds: float = 5.0,
) -> AttachmentTransportObservationResult:
    """Observe Composer attachment semantics without assigning files or dispatching Prompt text."""

    bound = session._bound_page()
    page = _identity_of(bound.ops) if bound is not None else None
    if page is None:
        return AttachmentTransportObservationResult(
            status=WebOperationStatus.FAILED, stage="session", page=None,
            failure=FailureKind.SESSION_BINDING_FAILURE,
            reason="No bound ChatGPT page is available for attachment transport observation.",
            evidence=WebDebugEvidence("composer.attachment.observe", ("session=unbound",)),
        )
    raw = session._observe_bound_composer_attachment_transport(
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=max(250, int(timeout_seconds * 1000)),
    )
    stage = str(raw.get("stage") or "unknown")
    reason = str(raw.get("reason") or "unknown")
    observations = (
        f"transport={raw.get('transport') or 'unknown'}", f"stage={stage}", f"reason={reason}",
        f"action_root_reason={raw.get('actionRootReason')}",
        f"action_root_candidate_count={int(raw.get('actionRootCandidateCount') or 0)}",
        f"action_root_selected_depth={raw.get('actionRootSelectedDepth')}",
        f"attachment_action_reason={raw.get('attachmentActionReason')}",
        f"attachment_raw_action_count={int(raw.get('attachmentRawActionCount') or 0)}",
        f"attachment_active_action_count={int(raw.get('attachmentActiveActionCount') or 0)}",
        f"attachment_logical_action_count={int(raw.get('attachmentLogicalActionCount') or 0)}",
        f"attachment_selected_kind={raw.get('attachmentSelectedKind')}",
        f"attachment_selected_signature={raw.get('attachmentSelectedSignature')}",
        f"attachment_click_reason={raw.get('attachmentClickReason')}",
        f"attachment_trigger_aria_controls={raw.get('attachmentTriggerAriaControls') or ''}",
        f"attachment_trigger_aria_owns={raw.get('attachmentTriggerAriaOwns') or ''}",
        f"popup_discovery_mode={raw.get('popupDiscoveryMode')}",
        f"popup_before_root_count={int(raw.get('popupBeforeRootCount') or 0)}",
        f"popup_after_root_count={int(raw.get('popupAfterRootCount') or 0)}",
        f"popup_new_root_count={int(raw.get('popupNewRootCount') or 0)}",
        f"popup_root_candidate_count={int(raw.get('popupRootCandidateCount') or 0)}",
        f"popup_selected_signature={raw.get('popupSelectedSignature')}",
        f"causal_observation_duration_ms={int(raw.get('causalObservationDurationMs') or 0)}",
        f"causal_poll_count={int(raw.get('causalPollCount') or 0)}",
        f"mutation_added_count={int(raw.get('mutationAddedCount') or 0)}",
        f"mutation_attribute_change_count={int(raw.get('mutationAttributeChangeCount') or 0)}",
        f"visible_interactive_before_count={int(raw.get('visibleInteractiveBeforeCount') or 0)}",
        f"visible_interactive_after_count={int(raw.get('visibleInteractiveAfterCount') or 0)}",
        f"newly_visible_interactive_count={int(raw.get('newlyVisibleInteractiveCount') or 0)}",
        f"popup_neighborhood_reason={raw.get('popupNeighborhoodReason')}",
        f"popup_neighborhood_selected_depth={raw.get('popupNeighborhoodSelectedDepth')}",
        f"popup_neighborhood_signature={raw.get('popupNeighborhoodSignature')}",
        f"menu_action_reason={raw.get('menuActionReason')}", f"menu_root_count={int(raw.get('menuRootCount') or 0)}",
        f"menu_raw_action_count={int(raw.get('menuRawActionCount') or 0)}",
        f"menu_active_action_count={int(raw.get('menuActiveActionCount') or 0)}",
        f"menu_logical_action_count={int(raw.get('menuLogicalActionCount') or 0)}",
        f"menu_selected_signature={raw.get('menuSelectedSignature')}",
        f"upload_activation_reason={raw.get('uploadActivationTargetReason')}",
        f"upload_activation_candidate_count={int(raw.get('uploadActivationTargetCandidateCount') or 0)}",
        f"upload_activation_selected_rank={raw.get('uploadActivationTargetSelectedRank')}",
        f"upload_activation_selected_source={raw.get('uploadActivationTargetSelectedSource')}",
        f"upload_activation_selected_tag={raw.get('uploadActivationTargetSelectedTag')}",
        f"upload_activation_selected_signature={raw.get('uploadActivationTargetSelectedSignature')}",
        f"activation_failure_mode={raw.get('activationFailureMode')}",
        f"activation_trusted_click_event_count={int(raw.get('activationTrustedClickEventCount') or 0)}",
        f"file_input_click_method_calls={int(raw.get('fileInputClickMethodCalls') or 0)}",
        f"file_input_show_picker_calls={int(raw.get('fileInputShowPickerCalls') or 0)}",
        f"show_open_file_picker_calls={int(raw.get('showOpenFilePickerCalls') or 0)}",
        f"file_input_before_count={int(raw.get('fileInputBeforeCount') or 0)}",
        f"file_input_after_count={int(raw.get('fileInputAfterCount') or 0)}",
        f"new_file_input_count={int(raw.get('newFileInputCount') or 0)}",
        f"activation_target_visible_after={bool(raw.get('activationTargetVisibleAfter'))}",
        f"picker_transport={raw.get('pickerTransport') or 'unknown'}",
        f"native_picker_api_observed={bool(raw.get('nativePickerApiObserved'))}",
        f"menu_click_reason={raw.get('menuClickReason')}",
        f"filechooser_event_observed={bool(raw.get('fileChooserEventObserved'))}",
        f"filechooser_multiple={raw.get('fileChooserMultiple')}", f"filechooser_accept={raw.get('fileChooserAccept') or ''}",
        f"file_assigned={bool(raw.get('fileAssigned'))}", f"prompt_dispatched={bool(raw.get('promptDispatched'))}",
    )
    details = {
        "transport": raw.get("transport"), "stage": stage, "reason": reason,
        "action_root": {"reason":raw.get("actionRootReason"),"candidate_count":int(raw.get("actionRootCandidateCount") or 0),"selected_depth":raw.get("actionRootSelectedDepth"),"ancestors":tuple(raw.get("actionRootDetails") or ())},
        "attachment_action": {"reason":raw.get("attachmentActionReason"),"raw_count":int(raw.get("attachmentRawActionCount") or 0),"active_count":int(raw.get("attachmentActiveActionCount") or 0),"logical_count":int(raw.get("attachmentLogicalActionCount") or 0),"selected_kind":raw.get("attachmentSelectedKind"),"selected_signature":raw.get("attachmentSelectedSignature"),"click_reason":raw.get("attachmentClickReason"),"trigger_aria_controls":raw.get("attachmentTriggerAriaControls") or "","trigger_aria_owns":raw.get("attachmentTriggerAriaOwns") or "","trigger_aria_expanded":raw.get("attachmentTriggerAriaExpanded") or "","trigger_rect":raw.get("attachmentTriggerRect"),"candidates":tuple(raw.get("attachmentActionDetails") or ())},
        "popup_root": {"discovery_mode":raw.get("popupDiscoveryMode"),"before_count":int(raw.get("popupBeforeRootCount") or 0),"after_count":int(raw.get("popupAfterRootCount") or 0),"new_count":int(raw.get("popupNewRootCount") or 0),"candidate_count":int(raw.get("popupRootCandidateCount") or 0),"selected_signature":raw.get("popupSelectedSignature"),"roots":tuple(raw.get("popupRootDetails") or ())},
        "causal_delta": {"observation_duration_ms":int(raw.get("causalObservationDurationMs") or 0),"poll_count":int(raw.get("causalPollCount") or 0),"mutation_added_count":int(raw.get("mutationAddedCount") or 0),"mutation_attribute_change_count":int(raw.get("mutationAttributeChangeCount") or 0),"mutation_samples":tuple(raw.get("mutationSamples") or ()),"visible_interactive_before_count":int(raw.get("visibleInteractiveBeforeCount") or 0),"visible_interactive_after_count":int(raw.get("visibleInteractiveAfterCount") or 0),"newly_visible_interactive_count":int(raw.get("newlyVisibleInteractiveCount") or 0),"newly_visible_interactives":tuple(raw.get("newlyVisibleInteractiveDetails") or ()),"popup_neighborhood_reason":raw.get("popupNeighborhoodReason"),"popup_neighborhood_selected_depth":raw.get("popupNeighborhoodSelectedDepth"),"popup_neighborhood_signature":raw.get("popupNeighborhoodSignature")},
        "upload_menu": {"reason":raw.get("menuActionReason"),"root_count":int(raw.get("menuRootCount") or 0),"raw_count":int(raw.get("menuRawActionCount") or 0),"active_count":int(raw.get("menuActiveActionCount") or 0),"logical_count":int(raw.get("menuLogicalActionCount") or 0),"selected_signature":raw.get("menuSelectedSignature"),"activation_target_reason":raw.get("uploadActivationTargetReason"),"activation_target_candidate_count":int(raw.get("uploadActivationTargetCandidateCount") or 0),"activation_target_selected_rank":raw.get("uploadActivationTargetSelectedRank"),"activation_target_selected_source":raw.get("uploadActivationTargetSelectedSource"),"activation_target_selected_tag":raw.get("uploadActivationTargetSelectedTag"),"activation_target_selected_role":raw.get("uploadActivationTargetSelectedRole"),"activation_target_selected_signature":raw.get("uploadActivationTargetSelectedSignature"),"activation_target_details":tuple(raw.get("uploadActivationTargetDetails") or ()),"click_reason":raw.get("menuClickReason"),"candidates":tuple(raw.get("menuActionDetails") or ())},
        "activation_diagnostic": {"arm_ok":bool(raw.get("activationDiagnosticArmOk")),"collect_ok":bool(raw.get("activationDiagnosticCollectOk")),"failure_mode":raw.get("activationFailureMode"),"duration_ms":int(raw.get("activationDiagnosticDurationMs") or 0),"target_outer_html":raw.get("activationTargetOuterHtml"),"target_center_hit_signature":raw.get("activationTargetCenterHitSignature"),"target_connected_after":bool(raw.get("activationTargetConnectedAfter")),"target_visible_after":bool(raw.get("activationTargetVisibleAfter")),"target_signature_after":raw.get("activationTargetSignatureAfter"),"center_hit_signature_after":raw.get("activationCenterHitSignatureAfter"),"active_element_signature":raw.get("activationActiveElementSignature"),"trusted_click_event_count":int(raw.get("activationTrustedClickEventCount") or 0),"file_input_click_event_count":int(raw.get("fileInputClickEventCount") or 0),"file_input_click_method_calls":int(raw.get("fileInputClickMethodCalls") or 0),"file_input_show_picker_calls":int(raw.get("fileInputShowPickerCalls") or 0),"show_open_file_picker_calls":int(raw.get("showOpenFilePickerCalls") or 0),"show_directory_picker_calls":int(raw.get("showDirectoryPickerCalls") or 0),"show_save_file_picker_calls":int(raw.get("showSaveFilePickerCalls") or 0),"show_open_file_picker_available":bool(raw.get("showOpenFilePickerAvailable")),"input_show_picker_available":bool(raw.get("inputShowPickerAvailable")),"file_input_before_count":int(raw.get("fileInputBeforeCount") or 0),"file_input_after_count":int(raw.get("fileInputAfterCount") or 0),"new_file_input_count":int(raw.get("newFileInputCount") or 0),"file_inputs_before":tuple(raw.get("fileInputsBefore") or ()),"file_inputs_after":tuple(raw.get("fileInputsAfter") or ()),"new_file_inputs":tuple(raw.get("newFileInputs") or ()),"event_trace":tuple(raw.get("activationEventTrace") or ())},
        "picker_transport": {"kind":raw.get("pickerTransport"),"native_api_observed":bool(raw.get("nativePickerApiObserved")),"filechooser_event_observed":bool(raw.get("fileChooserEventObserved"))},
        "filechooser": {"event_observed":bool(raw.get("fileChooserEventObserved")),"multiple":raw.get("fileChooserMultiple"),"accept":raw.get("fileChooserAccept") or ""},
        "file_assigned": bool(raw.get("fileAssigned")), "prompt_dispatched": bool(raw.get("promptDispatched")),
    }
    evidence = WebDebugEvidence("composer.attachment.observe", observations, details)
    if raw.get("ok") and (raw.get("fileChooserEventObserved") or raw.get("nativePickerApiObserved")):
        observed_stage = "picker-transport-observed"
        picker_kind = str(raw.get("pickerTransport") or ("playwright-filechooser" if raw.get("fileChooserEventObserved") else "unknown"))
        return AttachmentTransportObservationResult(
            status=WebOperationStatus.READY,
            stage=observed_stage,
            page=page,
            reason=f"Causal attachment path established picker transport {picker_kind}; no file was assigned and no Prompt was dispatched.",
            evidence=evidence,
        )
    ambiguous = (
        int(raw.get("attachmentLogicalActionCount") or 0) > 1
        or int(raw.get("menuLogicalActionCount") or 0) > 1
        or int(raw.get("popupRootCandidateCount") or 0) > 1
        or str(raw.get("reason") or "").endswith("ambiguous")
    )
    return AttachmentTransportObservationResult(status=WebOperationStatus.FAILED, stage=stage, page=page, failure=FailureKind.AMBIGUOUS_OBSERVATION if ambiguous else FailureKind.POSTCONDITION_NOT_ESTABLISHED, reason=f"Attachment transport observation stopped at {stage}: {reason}.", evidence=evidence)


def attach_files(
    session: ChatGPTSession,
    chat: "ChatIdentity",
    attachments: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.1,
) -> ComposerAttachmentResult:
    """Attach verified local context files and establish attachment-entity postconditions.

    The prompt is not written or sent by this function. Attachment assignment
    must succeed, then every declared filename must be correlated to a visible
    attachment entity through bounded Composer text/ARIA/title/data identity
    observation before the caller may proceed to prompt submission.
    """

    import hashlib
    import time
    from pathlib import Path

    items = tuple(attachments)
    if not items:
        return ComposerAttachmentResult(
            status=WebOperationStatus.FAILED,
            filenames=(),
            page=None,
            failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            reason="At least one attachment is required.",
            evidence=WebDebugEvidence("composer.attach", ("attachments=empty",)),
        )

    local_paths: list[str] = []
    filenames: list[str] = []
    observations: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return ComposerAttachmentResult(
                status=WebOperationStatus.FAILED,
                filenames=tuple(filenames),
                page=None,
                failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                reason=f"Attachment {index} must be a mapping.",
                evidence=WebDebugEvidence("composer.attach", tuple(observations)),
            )
        path = Path(str(item.get("path") or "")).expanduser().resolve()
        filename = str(item.get("filename") or "")
        declared_sha = str(item.get("sha256") or "")
        if not path.is_file() or not filename or path.name != filename:
            return ComposerAttachmentResult(
                status=WebOperationStatus.FAILED,
                filenames=tuple(filenames),
                page=None,
                failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                reason=f"Attachment {index} path/filename identity is invalid.",
                evidence=WebDebugEvidence("composer.attach", tuple(observations)),
            )
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha != declared_sha:
            return ComposerAttachmentResult(
                status=WebOperationStatus.FAILED,
                filenames=tuple(filenames),
                page=None,
                failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                reason=f"Attachment {filename} SHA-256 mismatch.",
                evidence=WebDebugEvidence("composer.attach", tuple(observations)),
            )
        local_paths.append(str(path))
        filenames.append(filename)
        observations.extend((f"attachment.{index}.filename={filename}", f"attachment.{index}.sha256={actual_sha}"))

    readiness = observe_composer_readiness(session, require_empty=True)
    if not readiness.ok:
        return ComposerAttachmentResult(
            status=WebOperationStatus.FAILED,
            filenames=tuple(filenames),
            page=readiness.page,
            failure=readiness.failure,
            reason=readiness.reason,
            evidence=readiness.evidence,
        )
    bound = session._bound_page()
    if bound is None:
        return ComposerAttachmentResult(
            status=WebOperationStatus.FAILED,
            filenames=tuple(filenames),
            page=readiness.page,
            failure=FailureKind.SESSION_BINDING_FAILURE,
            reason="Bound page disappeared before attachment upload.",
            evidence=WebDebugEvidence("composer.attach", tuple(observations + ["bound-page=missing"])),
        )

    try:
        baseline = bound.ops.evaluate(
            _ATTACHMENT_OBSERVE_SCRIPT,
            {"phase": "baseline", "expectedNames": filenames, "baselineSignatures": []},
        ) or {}
    except Exception as exc:
        return ComposerAttachmentResult(
            status=WebOperationStatus.FAILED,
            filenames=tuple(filenames),
            page=_identity_of(bound.ops),
            failure=FailureKind.UNKNOWN_WEB_STATE,
            reason=f"Attachment pre-upload entity baseline failed: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence("composer.attach", tuple(observations + ["attachment_baseline=exception"])),
        )
    baseline_signatures = tuple(str(value) for value in (baseline.get("candidateSignatures") or ()))
    observations.extend((
        f"attachment_baseline.root_reason={baseline.get('rootReason')}",
        f"attachment_baseline.scanned_visible_count={int(baseline.get('scannedVisibleCount') or 0)}",
        f"attachment_baseline.candidate_count={int(baseline.get('candidateCount') or 0)}",
        f"attachment_baseline.signature_count={len(baseline_signatures)}",
    ))

    action = session._upload_files_to_bound_composer(
        tuple(local_paths),
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=max(750, min(5000, int(max(0.0, timeout_seconds) * 1000))),
    ) or {}
    observations.extend((
        f"upload.ok={bool(action.get('ok'))}",
        f"upload.reason={action.get('reason')}",
        f"upload.transport={action.get('transport') or 'unknown'}",
        f"upload.stage={action.get('stage') or 'unknown'}",
        f"upload.attachment_action_reason={action.get('attachmentActionReason')}",
        f"upload.attachment_action_count={int(action.get('attachmentLogicalActionCount') or 0)}",
        f"upload.menu_action_reason={action.get('menuActionReason')}",
        f"upload.menu_action_count={int(action.get('menuLogicalActionCount') or 0)}",
        f"upload.activation_target_reason={action.get('uploadActivationTargetReason')}",
        f"upload.activation_target_source={action.get('uploadActivationTargetSelectedSource')}",
        f"upload.activation_target_tag={action.get('uploadActivationTargetSelectedTag')}",
        f"upload.activation_target_signature={action.get('uploadActivationTargetSelectedSignature')}",
        f"upload.activation_failure_mode={action.get('activationFailureMode')}",
        f"upload.activation_trusted_click_event_count={int(action.get('activationTrustedClickEventCount') or 0)}",
        f"upload.file_input_click_method_calls={int(action.get('fileInputClickMethodCalls') or 0)}",
        f"upload.file_input_show_picker_calls={int(action.get('fileInputShowPickerCalls') or 0)}",
        f"upload.show_open_file_picker_calls={int(action.get('showOpenFilePickerCalls') or 0)}",
        f"upload.file_input_before_count={int(action.get('fileInputBeforeCount') or 0)}",
        f"upload.file_input_after_count={int(action.get('fileInputAfterCount') or 0)}",
        f"upload.new_file_input_count={int(action.get('newFileInputCount') or 0)}",
        f"upload.picker_transport={action.get('pickerTransport') or 'unknown'}",
        f"upload.native_picker_bridge_arm_ok={bool(action.get('nativePickerBridgeArmOk'))}",
        f"upload.native_picker_bridge_calls={int(action.get('nativePickerBridgeCalls') or 0)}",
        f"upload.native_picker_bridge_get_file_calls={int(action.get('nativePickerBridgeGetFileCalls') or 0)}",
        f"upload.native_picker_bridge_returned_count={int(action.get('nativePickerBridgeReturnedCount') or 0)}",
        f"upload.filechooser_event_observed={bool(action.get('fileChooserEventObserved'))}",
        f"upload.filechooser_multiple={action.get('fileChooserMultiple')}",
        f"upload.filechooser_accept={action.get('fileChooserAccept') or ''}",
    ))
    if not action.get("ok"):
        return ComposerAttachmentResult(
            status=WebOperationStatus.FAILED,
            filenames=tuple(filenames),
            page=_identity_of(bound.ops),
            failure=(FailureKind.AMBIGUOUS_OBSERVATION if str(action.get("reason") or "").endswith("ambiguous") or int(action.get("attachmentLogicalActionCount") or 0) > 1 or int(action.get("menuLogicalActionCount") or 0) > 1 else FailureKind.POSTCONDITION_NOT_ESTABLISHED),
            reason="Composer attachment action did not establish a verified causal picker upload.",
            evidence=WebDebugEvidence("composer.attach", tuple(observations), dict(action)),
        )

    bridge_returned_names = tuple(str(name) for name in (action.get("nativePickerBridgeReturnedNames") or ()))
    bridge_identity_exact = (
        str(action.get("pickerTransport") or "") == "native-show-open-file-picker-bridge"
        and bridge_returned_names == tuple(filenames)
        and int(action.get("nativePickerBridgeReturnedCount") or 0) == len(filenames)
        and int(action.get("nativePickerBridgeGetFileCalls") or 0) >= len(filenames)
    )
    observations.extend((
        f"upload.native_picker_bridge_returned_names={','.join(bridge_returned_names)}",
        f"upload.native_picker_bridge_identity_exact={bridge_identity_exact}",
    ))

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last: dict[str, Any] = {}
    while True:
        try:
            last = bound.ops.evaluate(
                _ATTACHMENT_OBSERVE_SCRIPT,
                {"phase": "post", "expectedNames": filenames, "baselineSignatures": baseline_signatures},
            ) or {}
        except Exception as exc:
            return ComposerAttachmentResult(
                status=WebOperationStatus.FAILED,
                filenames=tuple(filenames),
                page=_identity_of(bound.ops),
                failure=FailureKind.UNKNOWN_WEB_STATE,
                reason=f"Attachment postcondition observation failed: {type(exc).__name__}: {exc}",
                evidence=WebDebugEvidence("composer.attach", tuple(observations + ["observe=exception"])),
            )
        if last.get("ok"):
            observations.extend((
                "attachment_postcondition=verified",
                "attachment_postcondition_mode=exact-entity-identity",
                f"attachment_entity.new_candidate_count={int(last.get('newCandidateCount') or 0)}",
                f"attachment_entity.new_attachment_candidate_count={int(last.get('newAttachmentCandidateCount') or 0)}",
                f"attachment_entity.observed_count={int(last.get('observedCount') or 0)}",
            ))
            return ComposerAttachmentResult(
                status=WebOperationStatus.READY,
                filenames=tuple(filenames),
                page=_identity_of(bound.ops),
                evidence=WebDebugEvidence(
                    "composer.attach",
                    tuple(observations),
                    {**dict(last), "upload": dict(action), "baseline": dict(baseline), "postcondition_mode": "exact-entity-identity"},
                ),
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, poll_interval_seconds))

    observations.extend((
        f"attachment_postcondition=not-established",
        f"attachment_entity.root_reason={last.get('rootReason')}",
        f"attachment_entity.scanned_visible_count={int(last.get('scannedVisibleCount') or 0)}",
        f"attachment_entity.candidate_count={int(last.get('candidateCount') or 0)}",
        f"attachment_entity.new_candidate_count={int(last.get('newCandidateCount') or 0)}",
        f"attachment_entity.new_attachment_candidate_count={int(last.get('newAttachmentCandidateCount') or 0)}",
        f"attachment_entity.observed_count={int(last.get('observedCount') or 0)}",
    ))
    return ComposerAttachmentResult(
        status=WebOperationStatus.FAILED,
        filenames=tuple(filenames),
        page=_identity_of(bound.ops),
        failure=FailureKind.TIMEOUT,
        reason="Attachment entity identity did not become observable in the bounded Composer neighborhood before timeout.",
        evidence=WebDebugEvidence(
            "composer.attach",
            tuple(observations),
            {**dict(last), "upload": dict(action), "baseline": dict(baseline), "postcondition_mode": "pending-entity-identity", "bridge_identity_exact": bridge_identity_exact},
        ),
    )


# Backward-compatible diagnostic aggregate for internal tests/readers.  Runtime
# submission deliberately evaluates the input and Send-action scripts in
# separate browser turns so React can commit the intermediate UI state.
_PROMPT_SUBMIT_SCRIPT = _PROMPT_INPUT_SCRIPT + "\n" + _SEND_ACTION_DISPATCH_SCRIPT


def _append_prompt_input_method_observations(
    observations: list[str],
    action: dict[str, Any],
    *,
    prefix: str,
) -> None:
    fields = (
        ("inputMethod", "method", str),
        ("inputSelectionEstablished", "selection_established", bool),
        ("inputLineBreakCount", "line_break_count", int),
        ("inputInsertTextCommandCount", "insert_text_command_count", int),
        ("inputInsertParagraphCommandCount", "insert_paragraph_command_count", int),
        ("inputBeforeInputEventCount", "beforeinput_event_count", int),
        ("inputEventCount", "input_event_count", int),
    )
    for source_key, evidence_key, caster in fields:
        value = action.get(source_key)
        if value is None:
            continue
        observations.append(f"{prefix}.{evidence_key}={caster(value)}")


def _append_prompt_correlation_observations(
    observations: list[str],
    action: dict[str, Any],
    *,
    prefix: str,
) -> None:
    fields = (
        ("inputTextLength", "text_length", int),
        ("inputRawTextLength", "raw_text_length", int),
        ("inputMatchesPrompt", "prompt_correlated", bool),
        ("inputNormalizationVersion", "normalization_version", str),
        ("inputSerializationVersion", "serialization_version", str),
        ("inputTextSource", "text_source", str),
        ("inputExpectedFingerprint", "expected_fingerprint", str),
        ("inputObservedFingerprint", "observed_fingerprint", str),
        ("inputFirstDifferenceIndex", "first_difference_index", int),
        ("inputExpectedCodePoint", "expected_codepoint", int),
        ("inputObservedCodePoint", "observed_codepoint", int),
    )
    for source_key, evidence_key, caster in fields:
        value = action.get(source_key)
        if value is None:
            continue
        observations.append(f"{prefix}.{evidence_key}={caster(value)}")


def submit_prompt(
    session: ChatGPTSession,
    chat: "ChatIdentity",
    prompt: str,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.1,
) -> "PromptSubmissionResult":
    """Submit a prompt and verify a *sent message* postcondition.

    This function never treats a DOM click as success.  It captures a message
    baseline, establishes exact Prompt correlation immediately before dispatch,
    then causally requires a new user message plus an emptied, ready Composer.
    Rendered user-message text equality is diagnostic only because Markdown/DOM
    rendering may be lossy.  Generation/assistant completion is intentionally owned by
    generation.py; callers needing the end-to-end domain operation should use
    submit_prompt_and_wait_for_response().
    """

    # Local imports avoid making composer readiness depend on message internals
    # during module import while keeping the package acyclic at runtime.
    import time

    from .messages import MessageObservationFailure, message_matches_prompt, observe_message_snapshot, promoted_chat_identity
    from .models import ChatIdentity, PromptSubmissionResult

    text = str(prompt)
    if not text.strip():
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=None,
            failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            reason="Prompt must not be empty.",
            evidence=WebDebugEvidence("composer.submit", ("prompt=empty",)),
        )

    readiness = observe_composer_readiness(session, require_empty=True)
    if not readiness.ok:
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=readiness.page,
            failure=readiness.failure,
            reason=readiness.reason,
            evidence=readiness.evidence,
        )

    baseline = observe_message_snapshot(session, expected_chat=chat)
    if isinstance(baseline, MessageObservationFailure):
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=readiness.page,
            failure=baseline.failure,
            reason=baseline.reason,
            evidence=baseline.evidence,
        )

    bound = session._bound_page()
    if bound is None:
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=readiness.page,
            failure=FailureKind.SESSION_BINDING_FAILURE,
            reason="Bound page disappeared before prompt submission.",
            evidence=WebDebugEvidence("composer.submit", ("bound-page=missing",)),
        )

    observations = list(readiness.evidence.observations if readiness.evidence is not None else ())
    observations.extend(
        (
            f"baseline.user_count={baseline.user_count}",
            f"baseline.assistant_count={baseline.assistant_count}",
        )
    )
    try:
        input_action = bound.ops.evaluate(_PROMPT_INPUT_SCRIPT, text) or {}
    except Exception as exc:
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=_identity_of(bound.ops),
            failure=exc.failure_kind if isinstance(exc, GuardError) else FailureKind.UNKNOWN_WEB_STATE,
            reason=exc.code if isinstance(exc, GuardError) else f"PROMPT_INPUT_FAILED:{type(exc).__name__}",
            evidence=WebDebugEvidence("composer.submit", tuple(observations + ["action.input=exception"])),
        )

    observations.append(f"action.input.ok={bool(input_action.get('ok'))}")
    observations.append(f"action.input.reason={input_action.get('reason')}")
    if input_action.get("candidateCount") is not None:
        observations.append(f"action.input.composer.raw_candidate_count={int(input_action.get('candidateCount') or 0)}")
    if input_action.get("activeCandidateCount") is not None:
        observations.append(f"action.input.composer.active_candidate_count={int(input_action.get('activeCandidateCount') or 0)}")
    if input_action.get("logicalCandidateCount") is not None:
        observations.append(f"action.input.composer.logical_candidate_count={int(input_action.get('logicalCandidateCount') or 0)}")
    if input_action.get("selectionReason") is not None:
        observations.append(f"action.input.composer.selection_reason={input_action.get('selectionReason')}")
    _append_prompt_input_method_observations(
        observations,
        input_action,
        prefix="action.input.native",
    )
    _append_prompt_correlation_observations(
        observations,
        input_action,
        prefix="action.input",
    )

    if not input_action.get("ok"):
        logical_count = int(input_action.get("logicalCandidateCount") or 0)
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=_identity_of(bound.ops),
            failure=(
                FailureKind.AMBIGUOUS_OBSERVATION
                if logical_count > 1
                else FailureKind.POSTCONDITION_NOT_ESTABLISHED
            ),
            reason="Prompt input postcondition could not be established safely.",
            evidence=WebDebugEvidence("composer.submit", tuple(observations), dict(input_action)),
        )

    action_deadline = time.monotonic() + max(0.0, timeout_seconds)
    send_attempts = 0
    action: dict[str, Any] = {}
    send_marker_token = f"shirabe-send-{time.monotonic_ns():x}"
    while True:
        send_attempts += 1
        try:
            action = bound.ops.evaluate(
                _SEND_ACTION_DISPATCH_SCRIPT,
                {"prompt": text, "token": send_marker_token},
            ) or {}
        except Exception as exc:
            return PromptSubmissionResult(
                status=WebOperationStatus.FAILED,
                chat=chat,
                user_message=None,
                page=_identity_of(bound.ops),
                failure=exc.failure_kind if isinstance(exc, GuardError) else FailureKind.UNKNOWN_WEB_STATE,
                reason=exc.code if isinstance(exc, GuardError) else f"SEND_ACTION_OBSERVATION_FAILED:{type(exc).__name__}",
                evidence=WebDebugEvidence(
                    "composer.submit",
                    tuple(observations + [f"action.send.wait_attempts={send_attempts}", "action.send=exception"]),
                ),
            )

        if action.get("ok"):
            break
        if not bool(action.get("retryable")):
            break
        if time.monotonic() >= action_deadline:
            break
        time.sleep(max(0.01, poll_interval_seconds))

    observations.append(f"action.send.wait_attempts={send_attempts}")
    observations.append(f"action.ok={bool(action.get('ok'))}")
    observations.append(f"action.reason={action.get('reason')}")
    if action.get("candidateCount") is not None:
        observations.append(f"action.composer.raw_candidate_count={int(action.get('candidateCount') or 0)}")
    if action.get("activeCandidateCount") is not None:
        observations.append(f"action.composer.active_candidate_count={int(action.get('activeCandidateCount') or 0)}")
    if action.get("logicalCandidateCount") is not None:
        observations.append(f"action.composer.logical_candidate_count={int(action.get('logicalCandidateCount') or 0)}")
    if action.get("selectionReason") is not None:
        observations.append(f"action.composer.selection_reason={action.get('selectionReason')}")
    _append_prompt_correlation_observations(
        observations,
        action,
        prefix="action.input.confirmed",
    )
    if action.get("sendRawCandidateCount") is not None:
        observations.append(f"action.send.raw_candidate_count={int(action.get('sendRawCandidateCount') or 0)}")
    if action.get("sendActiveCandidateCount") is not None:
        observations.append(f"action.send.active_candidate_count={int(action.get('sendActiveCandidateCount') or 0)}")
    if action.get("sendLogicalActionCount") is not None:
        observations.append(f"action.send.logical_action_count={int(action.get('sendLogicalActionCount') or 0)}")
    if action.get("sendSelectionReason") is not None:
        observations.append(f"action.send.selection_reason={action.get('sendSelectionReason')}")
    if action.get("sendActionKinds") is not None:
        observations.append(
            "action.send.candidate_kinds=" + ",".join(str(v) for v in (action.get("sendActionKinds") or []))
        )

    if not action.get("ok"):
        reason = str(action.get("reason") or "")
        logical_send_count = int(action.get("sendLogicalActionCount") or 0)
        if reason == "send-logical-action-count" and logical_send_count > 1:
            failure = FailureKind.AMBIGUOUS_OBSERVATION
            result_reason = "Multiple independent logical Send Actions remained after semantic filtering."
        elif bool(action.get("retryable")):
            failure = FailureKind.TIMEOUT
            result_reason = "Send Action did not become ready before the bounded wait expired."
        else:
            failure = FailureKind.POSTCONDITION_NOT_ESTABLISHED
            result_reason = "Prompt Send action could not be selected safely; no browser activation was attempted."
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=_identity_of(bound.ops),
            failure=failure,
            reason=result_reason,
            evidence=WebDebugEvidence("composer.submit", tuple(observations), dict(action)),
        )

    send_selector = f'[data-shirabe-send-action="{send_marker_token}"]'
    observations.append("action.send.activation_attempted=False")
    activation_error = None
    try:
        trusted_click = bound.ops.trusted_click_marked_action(
            send_selector,
            timeout_ms=max(250, int(max(0.25, timeout_seconds) * 1000)),
        )
    except Exception as exc:
        activation_error = exc
        original = getattr(exc, "original_result", None)
        if isinstance(original, dict) and "activationAttempted" in original:
            # A post-dispatch guard fault cannot erase the completed primitive
            # result. Copy it without changing the attached original object.
            trusted_click = dict(original)
        else:
            trusted_click = {
                "ok": False,
                # CALLBACK_ENTERED is only uncertainty, never evidence that
                # ChatGPT accepted the submission. Predispatch denial stays 0.
                "activationAttempted": bool(getattr(exc, "dispatched", False)),
                "trustedClick": False,
                "reason": "trusted-click-operation-raised", "errorType": type(exc).__name__,
            }
    if activation_error is None:
        try:
            bound.ops.evaluate(
                "payload=>{const el=document.querySelector('[data-shirabe-send-action=\"'+payload.token+'\"]');if(el)el.removeAttribute('data-shirabe-send-action');}",
                {"token": send_marker_token},
            )
        except Exception as exc:
            # Cleanup is a guarded mutation too. Its failure does not undo the
            # activation, and must not start a new observation/retry loop.
            activation_error = exc

    activation_attempted = bool(trusted_click.get("activationAttempted"))
    trusted_click_delivered = bool(trusted_click.get("trustedClick")) and bool(trusted_click.get("ok"))
    observations.extend((
        f"action.send.activation_attempted={activation_attempted}",
        f"action.send.trusted_click={trusted_click_delivered}",
        f"action.send.trusted_click_reason={trusted_click.get('reason')}",
        f"action.send.trusted_click_event_count={int(trusted_click.get('trustedClickEventCount') or 0)}",
        f"action.send.locator_count={int(trusted_click.get('locatorCount') or 0)}",
    ))

    def uncertain_submission(reason: str, *, error: Exception | None = None):
        try:
            session.require_reconciliation()
        except Exception:
            pass
        details = {
            "activation_attempted": activation_attempted,
            "trusted_click": trusted_click_delivered,
            "trusted_click_event_count": int(trusted_click.get("trustedClickEventCount") or 0),
            "web_submit_established": False,
            "send_state": "UNKNOWN" if activation_attempted else "NOT_DISPATCHED",
        }
        safe_observations = observations + [f"submission-uncertain={reason}"]
        if error is not None:
            safe_observations.append(f"error_type={type(error).__name__}")
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED, chat=chat, user_message=None,
            page=_identity_of(bound.ops), failure=FailureKind.RECONCILE_REQUIRED,
            reason="RECONCILIATION_REQUIRED",
            evidence=WebDebugEvidence("composer.submit", tuple(safe_observations), details),
        )

    if activation_error is not None:
        if activation_attempted:
            return uncertain_submission("activation-or-cleanup-observation-failed", error=activation_error)
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED, chat=chat, user_message=None,
            page=_identity_of(bound.ops),
            failure=activation_error.failure_kind if isinstance(activation_error, GuardError) else FailureKind.UNKNOWN_WEB_STATE,
            reason=activation_error.code if isinstance(activation_error, GuardError) else f"SEND_ACTIVATION_FAILED:{type(activation_error).__name__}",
            evidence=WebDebugEvidence("composer.submit", tuple(observations), {
                "activation_attempted": False, "trusted_click": False,
                "web_submit_established": False, "send_state": "NOT_DISPATCHED",
            }),
        )
    if not trusted_click_delivered:
        if activation_attempted:
            return uncertain_submission("trusted-activation-not-observed")
        details = dict(action)
        details.update({
            "activation_attempted": activation_attempted,
            "trusted_click": False,
            "trusted_click_reason": trusted_click.get("reason"),
            "trusted_click_event_count": int(trusted_click.get("trustedClickEventCount") or 0),
            "send_locator_count": int(trusted_click.get("locatorCount") or 0),
            "web_submit_established": False,
        })
        return PromptSubmissionResult(
            status=WebOperationStatus.FAILED,
            chat=chat,
            user_message=None,
            page=_identity_of(bound.ops),
            failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            reason="Trusted Send activation was not established; Web submit was not claimed.",
            evidence=WebDebugEvidence("composer.submit", tuple(observations), details),
        )

    # The prompt/content correlation is established *before* trusted activation against the
    # exact logical Composer text.  The rendered user-message surface is lossy
    # (Markdown/DOM rendering may alter whitespace), so it is retained as a
    # diagnostic only and is not used as the causal submission gate.
    dispatch_match_value = action.get("inputMatchesPrompt")
    dispatch_correlation_source = "send-action-exact"
    if dispatch_match_value is None:
        dispatch_match_value = input_action.get("inputMatchesPrompt")
        dispatch_correlation_source = "input-action-exact"
    if dispatch_match_value is None:
        # Compatibility for older fake transports.  Production browser scripts
        # always emit inputMatchesPrompt before action-dispatched.
        dispatch_match_value = bool(input_action.get("ok") and action.get("ok"))
        dispatch_correlation_source = "legacy-ok-fallback"

    dispatch_expected_fingerprint = action.get("inputExpectedFingerprint")
    if dispatch_expected_fingerprint is None:
        dispatch_expected_fingerprint = input_action.get("inputExpectedFingerprint")
    dispatch_observed_fingerprint = action.get("inputObservedFingerprint")
    if dispatch_observed_fingerprint is None:
        dispatch_observed_fingerprint = input_action.get("inputObservedFingerprint")
    dispatch_fingerprint_match = (
        dispatch_expected_fingerprint is None
        or dispatch_observed_fingerprint is None
        or str(dispatch_expected_fingerprint) == str(dispatch_observed_fingerprint)
    )
    dispatch_prompt_correlated = bool(dispatch_match_value) and dispatch_fingerprint_match
    observations.extend(
        (
            f"postcondition.dispatch_prompt_correlated={dispatch_prompt_correlated}",
            f"postcondition.dispatch_prompt_fingerprint_match={dispatch_fingerprint_match}",
            f"postcondition.dispatch_prompt_correlation_source={dispatch_correlation_source}",
        )
    )
    if dispatch_expected_fingerprint is not None:
        observations.append(f"postcondition.dispatch_prompt_fingerprint={dispatch_expected_fingerprint}")

    if not dispatch_prompt_correlated:
        return uncertain_submission("dispatch-prompt-correlation-not-established")

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_snapshot = baseline
    last_user_advanced = False
    last_latest_user_present = False
    last_rendered_user_matches_prompt = False
    last_composer_state: ComposerState | None = None
    last_composer_candidate_count: int = 0
    last_composer_observation_failed = False

    while True:
        observed = observe_message_snapshot(session, expected_chat=chat)
        if isinstance(observed, MessageObservationFailure):
            observations.append(f"message-observation-failure={observed.failure.value}")
            return uncertain_submission("post-send-message-observation-failed")
        else:
            last_snapshot = observed
            latest_user = observed.latest_user
            user_advanced = observed.user_count > baseline.user_count or (
                latest_user is not None
                and baseline.latest_user is not None
                and latest_user.identity.semantic_text_sha256 != baseline.latest_user.identity.semantic_text_sha256
            ) or (baseline.latest_user is None and latest_user is not None)
            rendered_user_matches_prompt = message_matches_prompt(latest_user, text)

            try:
                composer_state, composer_candidate_count = _observe_composer(bound.ops)
                composer_observation_failed = False
            except Exception as exc:
                return uncertain_submission("post-send-composer-observation-failed", error=exc)

            composer_cleared = (
                composer_state is not None
                and composer_candidate_count == 1
                and composer_state.empty
            )

            last_user_advanced = user_advanced
            last_latest_user_present = latest_user is not None
            last_rendered_user_matches_prompt = rendered_user_matches_prompt
            last_composer_state = composer_state
            last_composer_candidate_count = composer_candidate_count
            last_composer_observation_failed = composer_observation_failed

            causal_submission_established = (
                dispatch_prompt_correlated
                and trusted_click_delivered
                and user_advanced
                and latest_user is not None
                and composer_cleared
            )
            if causal_submission_established:
                durable_chat = promoted_chat_identity(chat, observed, url=bound.ops.url)
                raw_count = int(getattr(composer_candidate_count, "raw_count", int(composer_candidate_count)))
                active_count = int(getattr(composer_candidate_count, "active_count", int(composer_candidate_count)))
                selection_reason = str(getattr(composer_candidate_count, "selection_reason", "unknown"))
                observations.extend(
                    (
                        f"postcondition.user_count={observed.user_count}",
                        f"postcondition.user_advanced={user_advanced}",
                        f"postcondition.latest_user_present={latest_user is not None}",
                        f"postcondition.rendered_user_matches_prompt={rendered_user_matches_prompt}",
                        f"postcondition.composer.raw_candidate_count={raw_count}",
                        f"postcondition.composer.active_candidate_count={active_count}",
                        f"postcondition.composer.logical_candidate_count={int(composer_candidate_count)}",
                        f"postcondition.composer.selection_reason={selection_reason}",
                        f"postcondition.composer.empty={bool(composer_state and composer_state.empty)}",
                        f"postcondition.composer_cleared={composer_cleared}",
                        "postcondition=prompt-user-message-observed",
                        "postcondition=composer-empty-after-send",
                        "postcondition=causal-prompt-submission-established",
                        f"conversation_id={observed.conversation_id}",
                    )
                )
                return PromptSubmissionResult(
                    status=WebOperationStatus.SUBMITTED,
                    chat=durable_chat,
                    user_message=latest_user,
                    page=_identity_of(bound.ops),
                    evidence=WebDebugEvidence(
                        "composer.submit",
                        tuple(observations),
                        {
                            "baseline_user_count": baseline.user_count,
                            "final_user_count": observed.user_count,
                            "dispatch_prompt_correlated": dispatch_prompt_correlated,
                            "user_advanced": user_advanced,
                            "latest_user_present": latest_user is not None,
                            "rendered_user_matches_prompt": rendered_user_matches_prompt,
                            "composer_cleared": composer_cleared,
                            "causal_submission_established": True,
                            "activation_attempted": activation_attempted,
                            "trusted_click": trusted_click_delivered,
                            "web_submit_established": True,
                            "send_raw_candidate_count": int(action.get("sendRawCandidateCount") or 0),
                            "send_active_candidate_count": int(action.get("sendActiveCandidateCount") or 0),
                            "send_logical_action_count": int(action.get("sendLogicalActionCount") or 0),
                        },
                    ),
                )

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, poll_interval_seconds))

    try:
        session.require_reconciliation()
    except Exception:
        pass
    raw_count = int(getattr(last_composer_candidate_count, "raw_count", int(last_composer_candidate_count)))
    active_count = int(getattr(last_composer_candidate_count, "active_count", int(last_composer_candidate_count)))
    selection_reason = str(getattr(last_composer_candidate_count, "selection_reason", "unknown"))
    last_composer_empty = bool(last_composer_state and last_composer_state.empty)
    last_composer_cleared = (
        last_composer_state is not None
        and last_composer_candidate_count == 1
        and last_composer_state.empty
    )
    observations.extend(
        (
            f"last.user_count={last_snapshot.user_count}",
            f"last.assistant_count={last_snapshot.assistant_count}",
            f"postcondition.user_advanced={last_user_advanced}",
            f"postcondition.latest_user_present={last_latest_user_present}",
            f"postcondition.rendered_user_matches_prompt={last_rendered_user_matches_prompt}",
            f"postcondition.composer.observation_failed={last_composer_observation_failed}",
            f"postcondition.composer.raw_candidate_count={raw_count}",
            f"postcondition.composer.active_candidate_count={active_count}",
            f"postcondition.composer.logical_candidate_count={int(last_composer_candidate_count)}",
            f"postcondition.composer.selection_reason={selection_reason}",
            f"postcondition.composer.empty={last_composer_empty}",
            f"postcondition.composer_cleared={last_composer_cleared}",
            "postcondition.causal_submission_established=False",
            "postcondition=not-established",
        )
    )
    return PromptSubmissionResult(
        status=WebOperationStatus.FAILED,
        chat=chat,
        user_message=None,
        page=_identity_of(bound.ops),
        failure=FailureKind.TIMEOUT,
        reason="Trusted Send activation was attempted, but the causal Web submit postcondition was not established before timeout.",
        evidence=WebDebugEvidence(
            "composer.submit",
            tuple(observations),
            {
                "baseline_user_count": baseline.user_count,
                "final_user_count": last_snapshot.user_count,
                "dispatch_prompt_correlated": dispatch_prompt_correlated,
                "user_advanced": last_user_advanced,
                "latest_user_present": last_latest_user_present,
                "rendered_user_matches_prompt": last_rendered_user_matches_prompt,
                "composer_cleared": last_composer_cleared,
                "causal_submission_established": False,
                "activation_attempted": activation_attempted,
                "trusted_click": trusted_click_delivered,
                "web_submit_established": False,
                "send_raw_candidate_count": int(action.get("sendRawCandidateCount") or 0),
                "send_active_candidate_count": int(action.get("sendActiveCandidateCount") or 0),
                "send_logical_action_count": int(action.get("sendLogicalActionCount") or 0),
                "composer_raw_candidate_count": raw_count,
                "composer_active_candidate_count": active_count,
                "composer_logical_candidate_count": int(last_composer_candidate_count),
                "composer_empty": last_composer_empty,
            },
        ),
    )
