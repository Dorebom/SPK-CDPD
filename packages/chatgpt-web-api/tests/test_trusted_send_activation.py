from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chatgpt_web_api.composer import _SEND_ACTION_DISPATCH_SCRIPT
from chatgpt_web_api.evidence import debug_evidence_to_dict
from chatgpt_web_api.models import WebDebugEvidence
from chatgpt_web_api.session import _PlaywrightPageOps


class _Locator:
    def __init__(self, page: "_Page") -> None:
        self.page = page

    def count(self) -> int:
        return self.page.count

    def click(self, *, timeout: int) -> None:
        self.page.click_timeouts.append(timeout)
        self.page.click_count += 1
        if self.page.click_error:
            raise RuntimeError("click failed")


class _Page:
    def __init__(self, *, count: int = 1, trusted_events: int = 1, click_error: bool = False) -> None:
        self.count = count
        self.trusted_events = trusted_events
        self.click_error = click_error
        self.click_count = 0
        self.click_timeouts: list[int] = []
        self.url = "https://chatgpt.com/"

    def locator(self, selector: str) -> _Locator:
        assert selector == '[data-shirabe-send-action="token"]'
        return _Locator(self)

    def evaluate(self, expression: str, arg: Any = None) -> dict[str, Any]:
        if "SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_ARM_V1" in expression:
            return {"ok": True}
        if "SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_COLLECT_V1" in expression:
            return {
                "ok": True,
                "trustedClickEventCount": self.trusted_events,
                "clickEventCount": self.trusted_events,
                "eventTrace": [{"type": "click", "isTrusted": bool(self.trusted_events)}],
            }
        raise AssertionError(expression[:80])


class _Context:
    pass


def _ops(page: _Page) -> _PlaywrightPageOps:
    # This isolated primitive unit test supplies a trusted callback over a
    # synthetic Page. Strict admission/event/lease checks run in Session tests.
    def isolated_dispatch(name, callback, *, mutation, navigation=False):
        return callback()
    return _PlaywrightPageOps(_Context(), page, dispatch=isolated_dispatch)


def test_send_selection_script_marks_action_without_synthetic_click() -> None:
    assert "SHIRABE_SEND_ACTION_SELECT_V2" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "data-shirabe-send-action" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "selectedSend.button.click()" not in _SEND_ACTION_DISPATCH_SCRIPT


def test_trusted_send_activation_uses_unique_playwright_locator_click() -> None:
    page = _Page()
    result = _ops(page).trusted_click_marked_action(
        '[data-shirabe-send-action="token"]', timeout_ms=700
    )
    assert result["ok"] is True
    assert result["activationAttempted"] is True
    assert result["trustedClick"] is True
    assert result["trustedClickEventCount"] == 1
    assert page.click_count == 1
    assert page.click_timeouts == [700]


def test_trusted_send_activation_fails_closed_for_non_unique_marker() -> None:
    page = _Page(count=2)
    result = _ops(page).trusted_click_marked_action(
        '[data-shirabe-send-action="token"]', timeout_ms=700
    )
    assert result["ok"] is False
    assert result["activationAttempted"] is False
    assert result["trustedClick"] is False
    assert result["reason"] == "trusted-click-target-not-unique"
    assert page.click_count == 0


def test_trusted_send_activation_does_not_claim_trusted_click_when_event_missing() -> None:
    page = _Page(trusted_events=0)
    result = _ops(page).trusted_click_marked_action(
        '[data-shirabe-send-action="token"]', timeout_ms=700
    )
    assert result["ok"] is False
    assert result["activationAttempted"] is True
    assert result["trustedClick"] is False
    assert result["reason"] == "trusted-click-event-not-observed"


def test_causal_submission_debug_evidence_is_sanitized_and_structured() -> None:
    evidence = WebDebugEvidence(
        "composer.submit",
        ("postcondition.causal_submission_established=False",),
        {
            "activation_attempted": True,
            "trusted_click": True,
            "web_submit_established": False,
            "baseline_user_count": 3,
            "final_user_count": 3,
            "composer_cleared": False,
            "url": "https://chatgpt.com/path?token=secret",
        },
    )
    payload = debug_evidence_to_dict(evidence)
    assert payload["details"]["activation_attempted"] is True
    assert payload["details"]["trusted_click"] is True
    assert payload["details"]["web_submit_established"] is False
    assert payload["details"]["baseline_user_count"] == 3
    assert "secret" not in payload["details"]["url"]
