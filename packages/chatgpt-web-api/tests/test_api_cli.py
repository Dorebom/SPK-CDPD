from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatgpt_web_api import __main__ as cli
from chatgpt_web_api.api import (
    AttachmentTransportObservationSmokeResult,
    AttachmentVerticalSliceSmokeResult,
    ChatGPTWebAPI,
    VerticalSliceSmokeResult,
    _build_stage2_attachment_prompt,
    _verify_stage2_marker_zip,
)
from chatgpt_web_api.models import (
    ArtifactDownloadResult,
    AttachmentTransportObservationResult,
    ArtifactMetadata,
    AssistantResponse,
    ChatIdentity,
    FailureKind,
    GenerationState,
    MessageIdentity,
    MessageRole,
    ProjectIdentity,
    WebOperationEvidenceRecord,
    WebOperationStatus,
)


class FakeFacade:
    def __init__(self, result: VerticalSliceSmokeResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run_vertical_slice_smoke(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def run_attachment_transport_observation(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def run_attachment_vertical_slice_smoke(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _identity() -> MessageIdentity:
    return MessageIdentity(
        project_key="g-p-abc123",
        conversation_id="conv-1",
        role=MessageRole.ASSISTANT,
        role_index=0,
        semantic_text_sha256="s" * 64,
        message_id_sha256="m" * 64,
    )


def _passing_result(tmp_path: Path) -> VerticalSliceSmokeResult:
    identity = _identity()
    artifact = ArtifactMetadata(
        artifact_id="artifact-1",
        filename="M1-WEB-005_smoke.zip",
        saved_path=str(tmp_path / "M1-WEB-005_smoke.zip"),
        size_bytes=123,
        sha256="a" * 64,
        response_identity=identity,
        content_type="application/zip",
    )
    evidence = WebOperationEvidenceRecord(
        operation_id="op-1",
        operation="artifacts.download",
        outcome="DOWNLOADED",
        started_at="2026-08-11T00:00:00Z",
        completed_at="2026-08-11T00:00:01Z",
        project_key="g-p-abc123",
        conversation_id="conv-1",
        response_identity=identity,
        artifact=artifact,
    )
    download = ArtifactDownloadResult(
        status=WebOperationStatus.DOWNLOADED,
        artifact=artifact,
        response_identity=identity,
        page=None,
        operation_evidence=evidence,
    )
    return VerticalSliceSmokeResult(
        result="PASS",
        stages=(),
        project=ProjectIdentity("SPK-SHIRABE", "SPK-SHIRABE", "g-p-abc123", "https://chatgpt.com/g/g-p-abc123-spk-shirabe"),
        chat=ChatIdentity("g-p-abc123", "https://chatgpt.com/g/g-p-abc123-spk-shirabe/c/conv-1", conversation_id="conv-1"),
        generation_state=GenerationState.COMPLETED,
        response=AssistantResponse(identity=identity, text="done"),
        artifact_result=download,
    )


def test_vertical_slice_result_serialization_does_not_emit_raw_response_text(tmp_path: Path) -> None:
    payload = _passing_result(tmp_path).to_dict()
    assert payload["result"] == "PASS"
    assert payload["response"]["text_length"] == 4
    assert "text" not in payload["response"]
    assert payload["artifact_operation_evidence"]["artifact"]["filename"] == "M1-WEB-005_smoke.zip"


def test_cli_smoke_writes_report_and_returns_zero_for_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("make zip", encoding="utf-8")
    output = tmp_path / "out"
    facade = FakeFacade(_passing_result(output))

    monkeypatch.setattr(cli, "ChatGPTWebAPI", lambda cdp_url: facade)
    code = cli.main([
        "smoke",
        "--project-name", "SPK-SHIRABE",
        "--prompt-file", str(prompt),
        "--expected-artifact", "M1-WEB-005_smoke.zip",
        "--output-dir", str(output),
        "--print-json",
    ])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["result"] == "PASS"
    assert printed["checkpoint_b"] == "CANDIDATE_PASS_REQUIRES_EXTERNAL_REVIEW"
    assert Path(printed["report_path"]).is_file()
    assert facade.calls[0]["project_name"] == "SPK-SHIRABE"


def test_cli_smoke_returns_one_for_vertical_slice_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("make zip", encoding="utf-8")
    failed = VerticalSliceSmokeResult(
        result="FAIL",
        stages=(),
        failure_stage="S2",
        failure=FailureKind.TARGET_IDENTITY_MISMATCH,
        reason="wrong project",
    )
    facade = FakeFacade(failed)
    monkeypatch.setattr(cli, "ChatGPTWebAPI", lambda cdp_url: facade)
    code = cli.main([
        "smoke",
        "--project-name", "SPK-SHIRABE",
        "--prompt-file", str(prompt),
        "--expected-artifact", "M1-WEB-005_smoke.zip",
        "--output-dir", str(tmp_path / "out"),
        "--print-json",
    ])
    assert code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["failure_stage"] == "S2"
    assert printed["checkpoint_b"] == "NOT_ACHIEVED"


def test_cli_missing_prompt_file_returns_usage_error(tmp_path: Path, capsys) -> None:
    code = cli.main([
        "smoke",
        "--project-name", "SPK-SHIRABE",
        "--prompt-file", str(tmp_path / "missing.txt"),
        "--expected-artifact", "x.zip",
        "--output-dir", str(tmp_path / "out"),
    ])
    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_api_wait_for_artifact_retries_only_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    api = ChatGPTWebAPI(session=object())  # type: ignore[arg-type]
    response = AssistantResponse(identity=_identity(), text="done")
    calls = []

    class R:
        def __init__(self, failure=None, ok=False):
            self.failure = failure
            self.reason = "x"
            self.evidence = None
            self.ok = ok

    seq = [R(FailureKind.ARTIFACT_NOT_FOUND), R(ok=True)]
    monkeypatch.setattr("chatgpt_web_api.api.discover_zip_artifact", lambda *a, **k: calls.append(1) or seq.pop(0))
    monkeypatch.setattr("chatgpt_web_api.api.time.sleep", lambda *a, **k: None)
    result = api._wait_for_artifact(response, "x.zip", timeout_seconds=1, poll_interval_seconds=0)
    assert result.ok
    assert len(calls) == 2


def test_api_wait_for_artifact_fails_fast_on_ambiguity(monkeypatch: pytest.MonkeyPatch) -> None:
    api = ChatGPTWebAPI(session=object())  # type: ignore[arg-type]
    response = AssistantResponse(identity=_identity(), text="done")

    class R:
        failure = FailureKind.ARTIFACT_AMBIGUOUS
        reason = "ambiguous"
        evidence = None
        ok = False

    calls = []
    monkeypatch.setattr("chatgpt_web_api.api.discover_zip_artifact", lambda *a, **k: calls.append(1) or R())
    result = api._wait_for_artifact(response, "x.zip", timeout_seconds=99, poll_interval_seconds=0)
    assert result.failure == FailureKind.ARTIFACT_AMBIGUOUS
    assert len(calls) == 1


def test_cli_attachment_transport_observation_writes_immutable_diagnostic_and_never_claims_upload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    observation = AttachmentTransportObservationResult(
        status=WebOperationStatus.READY,
        stage="picker-transport-observed",
        page=None,
        reason="observed",
    )
    result = AttachmentTransportObservationSmokeResult(result="PASS", stages=(), observation=observation)
    facade = FakeFacade(result)
    monkeypatch.setattr(cli, "ChatGPTWebAPI", lambda cdp_url: facade)
    code = cli.main([
        "observe-attachment-transport",
        "--project-name", "SPK-SHIRABE",
        "--project-key", "g-p-abc123",
        "--output-dir", str(tmp_path / "out"),
        "--print-json",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["probe"] == "M1-WEBFIX-008_STAGE1_ATTACHMENT_TRANSPORT_OBSERVATION"
    assert payload["stage1_contract"]["file_assigned"] is False
    assert payload["stage1_contract"]["prompt_dispatched"] is False
    assert Path(payload["report_path"]).is_file()
    assert facade.calls[0]["project_name"] == "SPK-SHIRABE"


def test_stage2_cli_writes_report_for_native_picker_attachment_vertical_slice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    attachment = tmp_path / "M1-WEBFIX-009_attachment_smoke.md"
    marker = "WEBFIX009-ATTACHMENT-MARKER-V1"
    attachment.write_text(f"# smoke\nmarker: {marker}\n", encoding="utf-8")
    base = _passing_result(tmp_path)
    result = AttachmentVerticalSliceSmokeResult(
        result="PASS",
        stages=(),
        attachment_filename=attachment.name,
        expected_marker=marker,
        expected_artifact="M1-WEBFIX-009_attachment_smoke.zip",
        project=base.project,
        chat=base.chat,
        generation_state=base.generation_state,
        response=base.response,
        artifact_result=base.artifact_result,
        response_marker_verified=True,
        zip_marker_verified=True,
        reason="passed",
    )
    facade = FakeFacade(result)
    monkeypatch.setattr(cli, "ChatGPTWebAPI", lambda cdp_url: facade)
    code = cli.main([
        "smoke-attachment",
        "--project-name", "SPK-SHIRABE",
        "--project-key", "g-p-abc123",
        "--attachment-file", str(attachment),
        "--expected-marker", marker,
        "--expected-artifact", "M1-WEBFIX-009_attachment_smoke.zip",
        "--output-dir", str(tmp_path / "out"),
        "--print-json",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["probe"] == "M1-WEBFIX-009_STAGE2_NATIVE_PICKER_ATTACHMENT_VERTICAL_SLICE"
    assert payload["response_marker_verified"] is True
    assert payload["zip_marker_verified"] is True
    assert Path(payload["report_path"]).is_file()
    assert facade.calls[0]["attachment_file"] == attachment


def test_stage2_zip_marker_verification_requires_exact_root_marker_txt(tmp_path: Path) -> None:
    import zipfile

    marker = "WEBFIX009-ATTACHMENT-MARKER-V1"
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("marker.txt", marker + "\n")
    assert _verify_stage2_marker_zip(good, marker) == (True, None)

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("nested/marker.txt", marker + "\n")
    ok, reason = _verify_stage2_marker_zip(bad, marker)
    assert ok is False
    assert "exactly one marker.txt at archive root" in str(reason)


def test_stage2_instruction_requires_attachment_marker_without_embedding_marker_value() -> None:
    marker = "WEBFIX009-ATTACHMENT-MARKER-V1"
    prompt = _build_stage2_attachment_prompt("M1-WEBFIX-009_attachment_smoke.zip")
    assert "marker:" in prompt
    assert "attached Markdown" in prompt
    assert marker not in prompt


def test_cli_attachment_transport_observation_persists_failure_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    observation = AttachmentTransportObservationResult(
        status=WebOperationStatus.FAILED, stage="upload-menu-action-select", page=None,
        failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED, reason="upload-menu-action-not-found",
    )
    result = AttachmentTransportObservationSmokeResult(
        result="FAIL", stages=(), observation=observation, failure_stage="S4",
        failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED, reason=observation.reason,
    )
    facade = FakeFacade(result)
    monkeypatch.setattr(cli, "ChatGPTWebAPI", lambda cdp_url: facade)
    code = cli.main([
        "observe-attachment-transport", "--project-name", "SPK-SHIRABE",
        "--output-dir", str(tmp_path / "out"), "--print-json",
    ])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_stage"] == "S4"
    assert payload["stage1_contract"]["prompt_dispatched"] is False
    assert Path(payload["report_path"]).is_file()


def test_cli_attachment_transport_failure_captures_local_screenshot_when_session_supports_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    observation = AttachmentTransportObservationResult(
        status=WebOperationStatus.FAILED, stage="filechooser", page=None,
        failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED, reason="filechooser-not-observed",
    )
    result = AttachmentTransportObservationSmokeResult(
        result="FAIL", stages=(), observation=observation, failure_stage="S4",
        failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED, reason=observation.reason,
    )
    facade = FakeFacade(result)

    class ScreenshotSession:
        def capture_bound_screenshot(self, path) -> bool:
            Path(path).write_bytes(b"PNG")
            return True

    facade.session = ScreenshotSession()  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "ChatGPTWebAPI", lambda cdp_url: facade)
    code = cli.main([
        "observe-attachment-transport", "--project-name", "SPK-SHIRABE",
        "--output-dir", str(tmp_path / "out"), "--print-json",
    ])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic_screenshot"]["captured"] is True
    assert Path(payload["diagnostic_screenshot"]["path"]).read_bytes() == b"PNG"
    persisted = json.loads(Path(payload["report_path"]).read_text(encoding="utf-8"))
    assert persisted["diagnostic_screenshot"]["captured"] is True


def test_cli_stage2_failure_captures_local_screenshot_when_session_supports_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    attachment = tmp_path / "M1-WEBFIX-009_attachment_smoke.md"
    marker = "WEBFIX009-ATTACHMENT-MARKER-V1"
    attachment.write_text(f"# smoke\nmarker: {marker}\n", encoding="utf-8")
    base = _passing_result(tmp_path)
    result = AttachmentVerticalSliceSmokeResult(
        result="FAIL", stages=(), attachment_filename=attachment.name,
        expected_marker=marker, expected_artifact="M1-WEBFIX-009_attachment_smoke.zip",
        project=base.project, chat=base.chat, failure_stage="S4",
        failure=FailureKind.TIMEOUT, reason="attachment entity pending",
    )
    facade = FakeFacade(result)

    class ScreenshotSession:
        def capture_bound_screenshot(self, path) -> bool:
            Path(path).write_bytes(b"PNG")
            return True

    facade.session = ScreenshotSession()
    monkeypatch.setattr(cli, "ChatGPTWebAPI", lambda cdp_url: facade)
    code = cli.main([
        "smoke-attachment", "--project-name", "SPK-SHIRABE",
        "--attachment-file", str(attachment), "--expected-marker", marker,
        "--expected-artifact", "M1-WEBFIX-009_attachment_smoke.zip",
        "--output-dir", str(tmp_path / "out"), "--print-json",
    ])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic_screenshot"]["captured"] is True
    assert Path(payload["diagnostic_screenshot"]["path"]).read_bytes() == b"PNG"
    persisted = json.loads(Path(payload["report_path"]).read_text(encoding="utf-8"))
    assert persisted["diagnostic_screenshot"]["captured"] is True
