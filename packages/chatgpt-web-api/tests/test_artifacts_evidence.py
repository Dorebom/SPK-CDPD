from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatgpt_web_api.artifacts import _ARTIFACT_ACTION_SCRIPT, discover_zip_artifact, download_zip_artifact
from chatgpt_web_api.evidence import evidence_to_json, write_web_operation_evidence
from chatgpt_web_api.models import (
    AssistantResponse,
    FailureKind,
    MessageIdentity,
    MessageRole,
    WebOperationStatus,
)
from chatgpt_web_api.session import ChatGPTSession
from strict_fakes import make_bound_session, isolated_page_ops


@dataclass
class FakePage:
    _url: str
    artifact_observations: list[dict[str, Any]] = field(default_factory=list)
    fetch_results: list[dict[str, Any]] = field(default_factory=list)
    action_preflight_results: list[dict[str, Any]] = field(default_factory=list)
    action_download_results: list[dict[str, Any]] = field(default_factory=list)
    focused: bool = True
    _target_id: str = "target-1"

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return "ChatGPT"

    def target_id(self) -> str:
        return self._target_id

    def evaluate(self, expression: str, arg: Any | None = None) -> Any:
        if "document.visibilityState" in expression:
            return self.focused
        if "SHIRABE_ARTIFACT_DISCOVERY_V1" in expression:
            return _next(self.artifact_observations)
        if "SHIRABE_ARTIFACT_FETCH_V1" in expression:
            assert isinstance(arg, str)
            return _next(self.fetch_results)
        if "SHIRABE_ARTIFACT_ACTION_V1" in expression:
            assert isinstance(arg, dict) and arg.get("mode") == "preflight"
            return _next(self.action_preflight_results)
        raise AssertionError(f"unexpected evaluate script: {expression[:100]}")

    def goto(self, url: str, *, timeout_ms: int) -> None:
        self._url = url

    def download_from_action(
        self,
        expression: str,
        arg: Any,
        destination_path: str,
        *,
        expected_filename: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        assert "SHIRABE_ARTIFACT_ACTION_V1" in expression
        assert isinstance(arg, dict) and arg.get("mode") == "dispatch"
        result = dict(_next(self.action_download_results))
        body = result.pop("body", None)
        if result.get("ok") and body is not None:
            Path(destination_path).write_bytes(body)
        return result


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


def route() -> str:
    return "https://chatgpt.com/g/g-p-abc123-spk-shirabe/c/conv-1"


def semantic_hash(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.replace("\u200b", "").strip().splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def raw_id_hash(raw_id: str) -> str:
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()


def response(text: str = "done", *, raw_id: str = "a-target", index: int = 1) -> AssistantResponse:
    identity = MessageIdentity(
        project_key="g-p-abc123",
        conversation_id="conv-1",
        role=MessageRole.ASSISTANT,
        role_index=index,
        semantic_text_sha256=semantic_hash(text),
        message_id_sha256=raw_id_hash(raw_id),
    )
    return AssistantResponse(identity=identity, text=text)


def control(filename: str, href: str = "https://chatgpt.com/backend-api/estuary/file-1?sig=SECRET") -> dict[str, Any]:
    return {
        "controlIndex": 0,
        "tag": "a",
        "href": href,
        "download": filename,
        "text": filename,
        "ariaLabel": f"Download {filename}",
        "dataTestId": "artifact-download",
        "disabled": False,
    }


def row(index: int, text: str, raw_id: str, controls: list[dict[str, Any]]) -> dict[str, Any]:
    return {"assistantIndex": index, "text": text, "rawId": raw_id, "controls": controls}


def observation(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"url": route(), "assistantRows": list(rows)}


def zip_bytes(filename: str = "hello.txt", content: bytes = b"hello") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()


def fetch_result(body: bytes, *, filename: str = "result.zip", content_length: int | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "status": 200,
        "contentType": "application/zip",
        "contentDisposition": f'attachment; filename="{filename}"',
        "contentLength": str(len(body) if content_length is None else content_length),
        "finalUrl": "https://chatgpt.com/backend-api/estuary/file-1?sig=SECRET",
        "bodyBase64": base64.b64encode(body).decode("ascii"),
    }


def session(page: FakePage) -> ChatGPTSession:
    return make_bound_session(transport=FakeTransport([page]))


def test_discovery_binds_only_to_target_assistant_response() -> None:
    page = FakePage(
        route(),
        artifact_observations=[
            observation(
                row(0, "old", "a-old", [control("old.zip", "https://chatgpt.com/backend-api/estuary/old")]),
                row(1, "done", "a-target", [control("result.zip")]),
            )
        ],
    )
    result = discover_zip_artifact(session(page), response(), expected_filename="result.zip")
    assert result.ok
    assert result.candidate is not None
    assert result.candidate.filename == "result.zip"
    assert result.candidate.response_identity.message_id_sha256 == response().identity.message_id_sha256


def test_discovery_rejects_wrong_response_identity_instead_of_using_other_artifact() -> None:
    page = FakePage(
        route(),
        artifact_observations=[observation(row(0, "other", "a-other", [control("result.zip")]))],
    )
    result = discover_zip_artifact(session(page), response())
    assert not result.ok
    assert result.failure == FailureKind.TARGET_IDENTITY_MISMATCH


def test_ambiguous_file_like_control_fails_closed() -> None:
    ambiguous_button = {
        "controlIndex": 1,
        "tag": "button",
        "href": None,
        "download": None,
        "text": "Download artifact",
        "ariaLabel": "Download file",
        "dataTestId": "file-control",
        "disabled": False,
    }
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [control("result.zip"), ambiguous_button]))],
    )
    result = discover_zip_artifact(session(page), response())
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS


def test_multiple_zip_candidates_fail_closed() -> None:
    page = FakePage(
        route(),
        artifact_observations=[
            observation(row(1, "done", "a-target", [control("a.zip"), control("b.zip", "https://chatgpt.com/backend-api/estuary/file-2")]))
        ],
    )
    result = discover_zip_artifact(session(page), response())
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS


def test_download_saves_zip_with_size_sha256_and_response_identity(tmp_path: Path) -> None:
    body = zip_bytes()
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [control("result.zip")]))],
        fetch_results=[fetch_result(body)],
    )
    result = download_zip_artifact(
        session(page), response(), tmp_path, expected_filename="result.zip", operation_id_factory=lambda: "op-1"
    )
    assert result.ok
    assert result.status == WebOperationStatus.DOWNLOADED
    assert result.artifact is not None
    assert result.artifact.filename == "result.zip"
    assert result.artifact.size_bytes == len(body)
    assert result.artifact.sha256 == hashlib.sha256(body).hexdigest()
    assert Path(result.artifact.saved_path).read_bytes() == body
    assert result.artifact.response_identity == response().identity
    assert result.operation_evidence is not None
    assert result.operation_evidence.operation_id == "op-1"
    assert result.operation_evidence.artifact is not None
    assert result.evidence is not None
    assert "artifact_transport=direct-href" in result.evidence.observations


def test_existing_filename_collision_is_failure_and_not_overwritten(tmp_path: Path) -> None:
    original = b"do-not-overwrite"
    (tmp_path / "result.zip").write_bytes(original)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [control("result.zip")]))],
    )
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename="result.zip")
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_COLLISION
    assert (tmp_path / "result.zip").read_bytes() == original


def test_declared_length_mismatch_is_incomplete_and_no_file_is_saved(tmp_path: Path) -> None:
    body = zip_bytes()
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [control("result.zip")]))],
        fetch_results=[fetch_result(body, content_length=len(body) + 1)],
    )
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename="result.zip")
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_INCOMPLETE
    assert not (tmp_path / "result.zip").exists()


def test_invalid_zip_body_is_incomplete_and_no_file_is_saved(tmp_path: Path) -> None:
    body = b"not-a-zip"
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [control("result.zip")]))],
        fetch_results=[fetch_result(body)],
    )
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename="result.zip")
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_INCOMPLETE
    assert not (tmp_path / "result.zip").exists()


def test_evidence_serialization_does_not_persist_signed_url_and_is_immutable(tmp_path: Path) -> None:
    body = zip_bytes()
    signed = "https://chatgpt.com/backend-api/estuary/file-1?sig=TOP_SECRET_TOKEN"
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [control("result.zip", signed)]))],
        fetch_results=[fetch_result(body)],
    )
    result = download_zip_artifact(
        session(page), response(), tmp_path / "downloads", expected_filename="result.zip", operation_id_factory=lambda: "op-safe"
    )
    assert result.ok and result.operation_evidence is not None
    text = evidence_to_json(result.operation_evidence)
    assert "TOP_SECRET_TOKEN" not in text
    assert "backend-api/estuary" not in text
    assert result.artifact is not None and result.artifact.sha256 in text
    assert result.artifact.response_identity.semantic_text_sha256 in text

    evidence_path = tmp_path / "evidence" / "op-safe.json"
    write_web_operation_evidence(result.operation_evidence, evidence_path)
    loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert loaded["operation_id"] == "op-safe"
    try:
        write_web_operation_evidence(result.operation_evidence, evidence_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("evidence overwrite must fail")


def test_unsafe_expected_filename_is_rejected_before_browser_fetch(tmp_path: Path) -> None:
    page = FakePage(route())
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename="../escape.zip")
    assert not result.ok
    assert result.failure == FailureKind.UNSAFE_FILENAME


def test_web004_modules_do_not_import_shirabe_runtime() -> None:
    root = (Path(__file__).resolve().parents[1] / "src" / "chatgpt_web_api")
    for name in ("models.py", "artifacts.py", "evidence.py"):
        assert "shirabe_runtime" not in (root / name).read_text(encoding="utf-8")


def rich_control(
    *,
    index: int,
    tag: str = "button",
    href: str | None = None,
    related_hrefs: list[str] | None = None,
    download: str | None = None,
    text: str = "",
    aria: str = "",
    title: str = "",
    data_testid: str | None = None,
    role: str | None = "button",
    visible: bool = True,
    connected: bool = True,
    disabled: bool = False,
    container_key: str | None = None,
    container_artifact_hint: bool = False,
    container_text: str = "",
    container_testid: str | None = None,
) -> dict[str, Any]:
    return {
        "controlIndex": index,
        "tag": tag,
        "role": role,
        "href": href,
        "relatedHrefs": related_hrefs or [],
        "download": download,
        "text": text,
        "ariaLabel": aria,
        "title": title,
        "dataTestId": data_testid,
        "disabled": disabled,
        "connected": connected,
        "visible": visible,
        "containerKey": container_key,
        "containerArtifactHint": container_artifact_hint,
        "containerText": container_text,
        "containerDataTestId": container_testid,
        "containerTitle": "",
    }


def test_response_bound_artifact_card_button_and_link_deduplicate_to_one_expected_zip() -> None:
    href = "https://chatgpt.com/backend-api/estuary/file-expected?sig=SECRET"
    filename = "M1-WEB-005_smoke.zip"
    controls = [
        rich_control(
            index=0,
            tag="button",
            related_hrefs=[href],
            aria="Download",
            data_testid="artifact-download-button",
            container_key="response-container-0",
            container_artifact_hint=True,
            container_text=f"{filename}\nDownload",
            container_testid="artifact-card",
        ),
        rich_control(
            index=1,
            tag="a",
            href=href,
            related_hrefs=[href],
            text=filename,
            data_testid="artifact-link",
            role=None,
            container_key="response-container-0",
            container_artifact_hint=True,
            container_text=f"{filename}\nDownload",
            container_testid="artifact-card",
        ),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert result.ok
    assert result.candidate is not None
    assert result.candidate.filename == filename
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "raw_control_count=2" in observed
    assert "active_artifact_like_count=2" in observed
    assert "classified_zip_count=2" in observed
    assert "logical_artifact_count=1" in observed
    assert "exact_expected_match_count=1" in observed
    assert "ambiguous_control_count=0" in observed
    assert "selection_reason=single-exact-expected-logical-artifact" in observed


def test_non_artifact_response_actions_are_not_artifact_candidates() -> None:
    filename = "result.zip"
    href = "https://chatgpt.com/backend-api/estuary/file-1?sig=SECRET"
    controls = [
        rich_control(index=0, aria="Copy", data_testid="copy-turn-action"),
        rich_control(index=1, aria="Read aloud", data_testid="read-aloud-turn-action"),
        rich_control(
            index=2,
            tag="a",
            href=href,
            related_hrefs=[href],
            download=filename,
            text=filename,
            role=None,
            container_key="response-container-0",
            container_artifact_hint=True,
            container_text=filename,
            container_testid="artifact-card",
        ),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert result.ok
    assert result.evidence is not None
    observations = "\n".join(result.evidence.observations)
    assert "control.0.semantic=non-artifact-response-action" in observations
    assert "control.1.semantic=non-artifact-response-action" in observations
    assert "active_artifact_like_count=1" in observations


def test_two_independent_controls_for_same_expected_filename_remain_ambiguous() -> None:
    filename = "result.zip"
    controls = [
        rich_control(
            index=0,
            tag="a",
            href="https://chatgpt.com/backend-api/estuary/file-1?sig=A",
            download=filename,
            text=filename,
            role=None,
            container_key="response-container-0",
            container_artifact_hint=True,
            container_text=filename,
        ),
        rich_control(
            index=1,
            tag="a",
            href="https://chatgpt.com/backend-api/estuary/file-2?sig=B",
            download=filename,
            text=filename,
            role=None,
            container_key="response-container-1",
            container_artifact_hint=True,
            container_text=filename,
        ),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    observations = set(result.evidence.observations)
    assert "logical_artifact_count=2" in observations
    assert "exact_expected_match_count=2" in observations
    assert "selection_reason=multiple-exact-expected-logical-artifacts" in observations


def test_expected_filename_not_found_is_distinct_from_ambiguity() -> None:
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [control("other.zip")]))],
    )

    result = discover_zip_artifact(session(page), response(), expected_filename="result.zip")

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_NOT_FOUND
    assert result.evidence is not None
    observations = set(result.evidence.observations)
    assert "logical_artifact_count=1" in observations
    assert "exact_expected_match_count=0" in observations
    assert "selection_reason=expected-logical-artifact-not-found" in observations


def test_unresolved_generic_download_control_remains_ambiguous_for_expected_artifact() -> None:
    generic = rich_control(
        index=0,
        aria="Download file",
        data_testid="file-control",
        container_artifact_hint=False,
    )
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", [generic]))])

    result = discover_zip_artifact(session(page), response(), expected_filename="result.zip")

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    observations = set(result.evidence.observations)
    assert "ambiguous_control_count=1" in observations
    assert "selection_reason=unresolved-artifact-control-ambiguity" in observations


def test_inactive_artifact_controls_do_not_create_logical_candidates() -> None:
    filename = "result.zip"
    hidden = rich_control(
        index=0,
        tag="a",
        href="https://chatgpt.com/backend-api/estuary/hidden",
        download=filename,
        text=filename,
        role=None,
        visible=False,
    )
    disconnected = rich_control(
        index=1,
        tag="a",
        href="https://chatgpt.com/backend-api/estuary/disconnected",
        download=filename,
        text=filename,
        role=None,
        connected=False,
    )
    live = rich_control(
        index=2,
        tag="a",
        href="https://chatgpt.com/backend-api/estuary/live",
        download=filename,
        text=filename,
        role=None,
    )
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", [hidden, disconnected, live]))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert result.ok
    assert result.evidence is not None
    observations = "\n".join(result.evidence.observations)
    assert "active_artifact_like_count=1" in observations
    assert "control.0.semantic=inactive-control" in observations
    assert "control.1.semantic=inactive-control" in observations


def test_artifact_diagnostics_do_not_persist_signed_href_tokens() -> None:
    secret = "TOP_SECRET_QUERY_TOKEN"
    filename = "result.zip"
    href = f"https://chatgpt.com/backend-api/estuary/file-1?sig={secret}"
    card = rich_control(
        index=0,
        tag="button",
        related_hrefs=[href],
        aria=f"Download {filename}",
        container_key="response-container-0",
        container_artifact_hint=True,
        container_text=filename,
        container_testid="artifact-card",
    )
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", [card]))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert result.ok
    assert result.evidence is not None
    serialized = json.dumps(
        {"observations": result.evidence.observations, "details": dict(result.evidence.details)},
        ensure_ascii=False,
    )
    assert secret not in serialized
    assert "backend-api/estuary" not in serialized
    assert "generated_href_count=1" in serialized


def test_href_less_exact_expected_button_binds_as_response_action_artifact() -> None:
    filename = "M1-WEB-005_smoke.zip"
    button = rich_control(
        index=0,
        tag="button",
        role=None,
        text=filename,
        container_key="response-container-0",
        container_artifact_hint=False,
        container_text=filename,
    )
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", [button]))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert result.ok
    assert result.candidate is not None
    assert result.candidate.filename == filename
    assert result.candidate.candidate_class == "response-action"
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "active_artifact_like_count=1" in observed
    assert "classified_zip_count=1" in observed
    assert "logical_artifact_count=1" in observed
    assert "action_bound_artifact_count=1" in observed
    assert "exact_expected_match_count=1" in observed
    assert "ambiguous_control_count=0" in observed
    assert "selection_reason=single-exact-expected-action-artifact" in observed
    assert "control.0.semantic=logical-action-artifact-candidate" in observed
    assert "control.0.href_candidate_count=0" in observed
    assert "control.0.expected_filename_match=true" in observed


def test_exact_expected_action_absorbs_one_transferless_exact_stem_companion() -> None:
    filename = "M1-WEBFIX-009_attachment_smoke.zip"
    stem = Path(filename).stem
    controls = [
        rich_control(index=0, tag="button", role=None, text=stem),
        rich_control(index=1, tag="button", role=None, text=f"Download {filename}"),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert result.ok
    assert result.candidate is not None
    assert result.candidate.filename == filename
    assert result.candidate.candidate_class == "response-action"
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "exact_expected_match_count=1" in observed
    assert "stem_companion_candidate_count=1" in observed
    assert "absorbed_stem_companion_count=1" in observed
    assert "ambiguous_control_count=0" in observed
    assert "selection_reason=single-exact-expected-action-artifact-with-stem-companion" in observed
    assert "control.0.semantic=artifact-filename-not-unique" in observed
    assert "control.1.semantic=logical-action-artifact-candidate" in observed


def test_two_exact_stem_companions_remain_ambiguous() -> None:
    filename = "M1-WEBFIX-009_attachment_smoke.zip"
    stem = Path(filename).stem
    controls = [
        rich_control(index=0, tag="button", role=None, text=stem),
        rich_control(index=1, tag="button", role=None, text=stem),
        rich_control(index=2, tag="button", role=None, text=f"Download {filename}"),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "stem_companion_candidate_count=2" in observed
    assert "absorbed_stem_companion_count=0" in observed
    assert "ambiguous_control_count=2" in observed
    assert "selection_reason=unresolved-artifact-control-ambiguity" in observed


def test_transfer_bearing_exact_stem_control_is_not_absorbed() -> None:
    filename = "M1-WEBFIX-009_attachment_smoke.zip"
    stem = Path(filename).stem
    controls = [
        rich_control(
            index=0,
            tag="button",
            role=None,
            text=stem,
            related_hrefs=["https://chatgpt.com/backend-api/estuary/stem-control?sig=SECRET"],
        ),
        rich_control(index=1, tag="button", role=None, text=f"Download {filename}"),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "stem_companion_candidate_count=0" in observed
    assert "absorbed_stem_companion_count=0" in observed
    assert "ambiguous_control_count=1" in observed


def test_nonmatching_artifact_like_stem_control_remains_ambiguous() -> None:
    filename = "M1-WEBFIX-009_attachment_smoke.zip"
    controls = [
        rich_control(index=0, tag="button", role=None, text="M1-WEBFIX-009_attachment_other"),
        rich_control(index=1, tag="button", role=None, text=f"Download {filename}"),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "stem_companion_candidate_count=0" in observed
    assert "absorbed_stem_companion_count=0" in observed
    assert "ambiguous_control_count=1" in observed


def test_two_href_less_exact_expected_buttons_remain_ambiguous() -> None:
    filename = "result.zip"
    controls = [
        rich_control(index=0, tag="button", role=None, text=filename, container_key="response-container-0"),
        rich_control(index=1, tag="button", role=None, text=filename, container_key="response-container-1"),
    ]
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", controls))])

    result = discover_zip_artifact(session(page), response(), expected_filename=filename)

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "logical_artifact_count=2" in observed
    assert "action_bound_artifact_count=2" in observed
    assert "exact_expected_match_count=2" in observed
    assert "selection_reason=multiple-exact-expected-logical-artifacts" in observed


def test_href_less_zip_button_without_expected_filename_remains_fail_closed() -> None:
    button = rich_control(index=0, tag="button", role=None, text="result.zip")
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", [button]))])

    result = discover_zip_artifact(session(page), response())

    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "logical_artifact_count=0" in observed
    assert "ambiguous_control_count=1" in observed
    assert "control.0.semantic=artifact-href-not-established" in observed


def _action_preflight(
    *,
    response_matches: int = 1,
    action_candidates: int = 1,
    ok: bool = True,
    candidate_diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reason = "action-bound" if ok else ("action-candidate-not-found" if action_candidates == 0 else "action-candidate-ambiguous")
    return {
        "ok": ok,
        "reason": reason,
        "responseMatchCount": response_matches,
        "actionCandidateCount": action_candidates,
        "controlIndex": 0 if ok else None,
        "candidateDiagnostics": candidate_diagnostics or [],
    }


def test_action_preflight_script_uses_s8_control_universe_and_strict_artifact_correlation() -> None:
    assert 'a[href], a[download], button, [role="button"]' in _ARTIFACT_ACTION_SCRIPT
    assert "probe.querySelector('a[href]')" in _ARTIFACT_ACTION_SCRIPT
    assert "const expectedFilenameMatch = ownFilenameMatch || containerFilenameMatch" in _ARTIFACT_ACTION_SCRIPT
    assert "semantic === 'logical-action-artifact-candidate'" in _ARTIFACT_ACTION_SCRIPT
    # Retry 1 must not restore the old broad ancestor rule where any parent text
    # containing the filename promoted an unrelated response utility button.
    assert "if (exactFilenameIn(probe.innerText || probe.textContent || ''))" not in _ARTIFACT_ACTION_SCRIPT


def test_action_preflight_candidate_diagnostics_explain_coding_citation_exclusion(tmp_path: Path) -> None:
    filename = "result.zip"
    body = zip_bytes()
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [button]))],
        action_preflight_results=[
            _action_preflight(
                candidate_diagnostics=[
                    {
                        "index": 0, "tag": "button", "role": None, "visible": True, "enabled": True,
                        "connected": True, "actionable": True, "artifactLike": True,
                        "semantic": "logical-action-artifact-candidate", "ariaLabel": None, "title": None,
                        "dataTestId": None, "text": filename, "ownFilenameMatch": True,
                        "containerArtifactHint": False, "containerFilenameMatch": False,
                        "expectedFilenameMatch": True, "selectedCandidate": True,
                    },
                    {
                        "index": 1, "tag": "button", "role": None, "visible": True, "enabled": True,
                        "connected": True, "actionable": True, "artifactLike": False,
                        "semantic": "non-artifact-control", "ariaLabel": "Coding Citation", "title": None,
                        "dataTestId": None, "text": None, "ownFilenameMatch": False,
                        "containerArtifactHint": False, "containerFilenameMatch": False,
                        "expectedFilenameMatch": False, "selectedCandidate": False,
                    },
                ]
            )
        ],
        action_download_results=[_action_download_result(body=body)],
    )

    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename=filename)

    assert result.ok
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "action_preflight.action_candidate_count=1" in observed
    assert "action_preflight.observed_control_count=2" in observed
    assert "action_preflight.candidate.0.expected_filename_match=true" in observed
    assert "action_preflight.candidate.0.selected=true" in observed
    assert "action_preflight.candidate.1.aria_label=Coding Citation" in observed
    assert "action_preflight.candidate.1.artifact_like=false" in observed
    assert "action_preflight.candidate.1.expected_filename_match=false" in observed
    assert "action_preflight.candidate.1.selected=false" in observed


def _action_download_result(
    *,
    body: bytes | None = None,
    status: str = "completed",
    ok: bool = True,
    event_observed: bool = True,
    filename_match: bool | None = True,
    transfer_complete: bool = True,
    dispatch_ok: bool = True,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "status": status,
        "eventObserved": event_observed,
        "suggestedFilename": "result.zip" if event_observed else None,
        "suggestedFilenameMatch": filename_match,
        "transferComplete": transfer_complete,
        "dispatchResult": {"ok": dispatch_ok, "reason": "action-dispatched" if dispatch_ok else "action-candidate-not-found"},
        "body": body,
    }


def test_href_less_bound_artifact_downloads_via_response_action_and_verifies_integrity(tmp_path: Path) -> None:
    filename = "result.zip"
    body = zip_bytes()
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [button]))],
        action_preflight_results=[_action_preflight()],
        action_download_results=[_action_download_result(body=body)],
    )

    result = download_zip_artifact(
        session(page), response(), tmp_path, expected_filename=filename, operation_id_factory=lambda: "op-action"
    )

    assert result.ok
    assert result.status == WebOperationStatus.DOWNLOADED
    assert result.artifact is not None
    assert result.artifact.filename == filename
    assert result.artifact.size_bytes == len(body)
    assert result.artifact.sha256 == hashlib.sha256(body).hexdigest()
    assert (tmp_path / filename).read_bytes() == body
    assert not list(tmp_path.glob("*.part"))
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "artifact_transport=action" in observed
    assert "download_action_bound=true" in observed
    assert "download_event_observed=true" in observed
    assert "suggested_filename_match=true" in observed
    assert "transfer_complete=true" in observed
    assert "zip_integrity=true" in observed


def test_action_download_with_exact_stem_companion_reaches_response_action_transport(tmp_path: Path) -> None:
    filename = "M1-WEBFIX-009_attachment_smoke.zip"
    stem = Path(filename).stem
    body = zip_bytes("marker.txt", b"WEBFIX009-ATTACHMENT-MARKER-V1\n")
    controls = [
        rich_control(index=0, tag="button", role=None, text=stem),
        rich_control(index=1, tag="button", role=None, text=f"Download {filename}"),
    ]
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", controls))],
        action_preflight_results=[_action_preflight()],
        action_download_results=[_action_download_result(body=body)],
    )

    result = download_zip_artifact(
        session(page), response(), tmp_path, expected_filename=filename, operation_id_factory=lambda: "retry2-action"
    )

    assert result.ok
    assert result.artifact is not None
    assert result.artifact.filename == filename
    assert (tmp_path / filename).read_bytes() == body
    assert result.evidence is not None
    observed = set(result.evidence.observations)
    assert "absorbed_stem_companion_count=1" in observed
    assert "artifact_transport=action" in observed
    assert "zip_integrity=true" in observed


def test_action_download_timeout_is_distinct_and_saves_no_file(tmp_path: Path) -> None:
    filename = "result.zip"
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [button]))],
        action_preflight_results=[_action_preflight()],
        action_download_results=[_action_download_result(ok=False, status="timeout", event_observed=False, filename_match=None, transfer_complete=False)],
    )
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename=filename)
    assert not result.ok
    assert result.failure == FailureKind.TIMEOUT
    assert result.evidence is not None
    assert "download_transport_state=timeout" in result.evidence.observations
    assert "download_event_observed=false" in result.evidence.observations
    assert not (tmp_path / filename).exists()


def test_action_download_cancelled_is_distinct_in_evidence(tmp_path: Path) -> None:
    filename = "result.zip"
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [button]))],
        action_preflight_results=[_action_preflight()],
        action_download_results=[_action_download_result(ok=False, status="cancelled", transfer_complete=False)],
    )
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename=filename)
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_DOWNLOAD_FAILURE
    assert result.evidence is not None
    assert "download_transport_state=cancelled" in result.evidence.observations
    assert "transfer_complete=false" in result.evidence.observations


def test_action_download_suggested_filename_mismatch_fails_closed(tmp_path: Path) -> None:
    filename = "result.zip"
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [button]))],
        action_preflight_results=[_action_preflight()],
        action_download_results=[_action_download_result(ok=False, status="filename-mismatch", filename_match=False, transfer_complete=True)],
    )
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename=filename)
    assert not result.ok
    assert result.failure == FailureKind.TARGET_IDENTITY_MISMATCH
    assert result.evidence is not None
    assert "suggested_filename_match=false" in result.evidence.observations
    assert not (tmp_path / filename).exists()


def test_action_preflight_multiple_candidates_fails_before_download_dispatch(tmp_path: Path) -> None:
    filename = "result.zip"
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [button]))],
        action_preflight_results=[_action_preflight(action_candidates=2, ok=False)],
    )
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename=filename)
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert result.evidence is not None
    assert "download_action_bound=false" in result.evidence.observations
    assert not page.action_download_results


def test_action_download_corrupt_zip_is_incomplete_and_temp_is_removed(tmp_path: Path) -> None:
    filename = "result.zip"
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(
        route(),
        artifact_observations=[observation(row(1, "done", "a-target", [button]))],
        action_preflight_results=[_action_preflight()],
        action_download_results=[_action_download_result(body=b"broken-zip")],
    )
    result = download_zip_artifact(
        session(page), response(), tmp_path, expected_filename=filename, operation_id_factory=lambda: "op-bad"
    )
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_INCOMPLETE
    assert result.evidence is not None
    assert "zip_integrity=false" in result.evidence.observations
    assert not (tmp_path / filename).exists()
    assert not (tmp_path / f".{filename}.op-bad.part").exists()


def test_action_download_collision_fails_before_action_dispatch(tmp_path: Path) -> None:
    filename = "result.zip"
    original = b"keep-me"
    (tmp_path / filename).write_bytes(original)
    button = rich_control(index=0, tag="button", role=None, text=filename)
    page = FakePage(route(), artifact_observations=[observation(row(1, "done", "a-target", [button]))])
    result = download_zip_artifact(session(page), response(), tmp_path, expected_filename=filename)
    assert not result.ok
    assert result.failure == FailureKind.ARTIFACT_COLLISION
    assert (tmp_path / filename).read_bytes() == original
    assert not page.action_preflight_results


def test_playwright_action_download_wait_is_armed_before_dispatch(tmp_path: Path) -> None:
    from chatgpt_web_api.session import _PlaywrightPageOps

    events: list[str] = []
    body = zip_bytes()

    class FakeDownload:
        suggested_filename = "result.zip"

        def failure(self) -> None:
            events.append("failure-check")
            return None

        def save_as(self, path: str) -> None:
            events.append("save")
            Path(path).write_bytes(body)

        def cancel(self) -> None:
            events.append("cancel")

    class FakeDownloadInfo:
        value = FakeDownload()

    class FakeExpectDownload:
        def __enter__(self) -> FakeDownloadInfo:
            events.append("wait-enter")
            return FakeDownloadInfo()

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            events.append("wait-exit")
            return None

    class FakePlaywrightPage:
        url = route()

        def expect_download(self, *, timeout: int) -> FakeExpectDownload:
            assert timeout == 1234
            events.append("wait-created")
            return FakeExpectDownload()

        def evaluate(self, expression: str, arg: Any) -> dict[str, Any]:
            assert "SHIRABE_ARTIFACT_ACTION_V1" in expression
            assert arg["mode"] == "dispatch"
            events.append("dispatch")
            return {"ok": True, "reason": "action-dispatched"}

    ops = isolated_page_ops(object(), FakePlaywrightPage())
    destination = tmp_path / "transport.part"
    result = ops.download_from_action(
        "/* SHIRABE_ARTIFACT_ACTION_V1 */ (arg) => ({ok:true})",
        {"mode": "dispatch"},
        str(destination),
        expected_filename="result.zip",
        timeout_ms=1234,
    )

    assert result["ok"] is True
    assert result["eventObserved"] is True
    assert result["suggestedFilenameMatch"] is True
    assert result["transferComplete"] is True
    assert destination.read_bytes() == body
    assert events.index("wait-enter") < events.index("dispatch")
    assert events.index("dispatch") < events.index("wait-exit")


def test_playwright_action_download_filename_mismatch_does_not_save(tmp_path: Path) -> None:
    from chatgpt_web_api.session import _PlaywrightPageOps

    class FakeDownload:
        suggested_filename = "other.zip"

        def failure(self) -> None:
            return None

        def save_as(self, path: str) -> None:
            raise AssertionError("filename mismatch must fail before save_as")

        def cancel(self) -> None:
            return None

    class FakeDownloadInfo:
        value = FakeDownload()

    class FakeExpectDownload:
        def __enter__(self) -> FakeDownloadInfo:
            return FakeDownloadInfo()

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

    class FakePlaywrightPage:
        url = route()

        def expect_download(self, *, timeout: int) -> FakeExpectDownload:
            return FakeExpectDownload()

        def evaluate(self, expression: str, arg: Any) -> dict[str, Any]:
            return {"ok": True, "reason": "action-dispatched"}

    ops = isolated_page_ops(object(), FakePlaywrightPage())
    destination = tmp_path / "transport.part"
    result = ops.download_from_action(
        "/* SHIRABE_ARTIFACT_ACTION_V1 */ (arg) => ({ok:true})",
        {"mode": "dispatch"},
        str(destination),
        expected_filename="result.zip",
        timeout_ms=100,
    )

    assert result["ok"] is False
    assert result["status"] == "filename-mismatch"
    assert result["eventObserved"] is True
    assert result["suggestedFilenameMatch"] is False
    assert result["transferComplete"] is True
    assert not destination.exists()


def test_playwright_action_download_timeout_is_reported_without_save(tmp_path: Path) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from chatgpt_web_api.session import _PlaywrightPageOps

    class FakeDownloadInfo:
        value = None

    class FakeExpectDownload:
        def __enter__(self) -> FakeDownloadInfo:
            return FakeDownloadInfo()

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            raise PlaywrightTimeoutError("timeout")

    class FakePlaywrightPage:
        url = route()

        def expect_download(self, *, timeout: int) -> FakeExpectDownload:
            return FakeExpectDownload()

        def evaluate(self, expression: str, arg: Any) -> dict[str, Any]:
            return {"ok": True, "reason": "action-dispatched"}

    ops = isolated_page_ops(object(), FakePlaywrightPage())
    destination = tmp_path / "transport.part"
    result = ops.download_from_action(
        "/* SHIRABE_ARTIFACT_ACTION_V1 */ (arg) => ({ok:true})",
        {"mode": "dispatch"},
        str(destination),
        expected_filename="result.zip",
        timeout_ms=1,
    )
    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert result["eventObserved"] is False
    assert result["transferComplete"] is False
    assert not destination.exists()


def test_playwright_action_download_cancelled_transfer_is_reported(tmp_path: Path) -> None:
    from chatgpt_web_api.session import _PlaywrightPageOps

    class FakeDownload:
        suggested_filename = "result.zip"

        def failure(self) -> str:
            return "canceled"

        def save_as(self, path: str) -> None:
            raise AssertionError("cancelled transfer must not be saved")

        def cancel(self) -> None:
            return None

    class FakeDownloadInfo:
        value = FakeDownload()

    class FakeExpectDownload:
        def __enter__(self) -> FakeDownloadInfo:
            return FakeDownloadInfo()

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

    class FakePlaywrightPage:
        url = route()

        def expect_download(self, *, timeout: int) -> FakeExpectDownload:
            return FakeExpectDownload()

        def evaluate(self, expression: str, arg: Any) -> dict[str, Any]:
            return {"ok": True, "reason": "action-dispatched"}

    ops = isolated_page_ops(object(), FakePlaywrightPage())
    destination = tmp_path / "transport.part"
    result = ops.download_from_action(
        "/* SHIRABE_ARTIFACT_ACTION_V1 */ (arg) => ({ok:true})",
        {"mode": "dispatch"},
        str(destination),
        expected_filename="result.zip",
        timeout_ms=100,
    )
    assert result["ok"] is False
    assert result["status"] == "cancelled"
    assert result["eventObserved"] is True
    assert result["suggestedFilenameMatch"] is True
    assert result["transferComplete"] is False
    assert not destination.exists()
