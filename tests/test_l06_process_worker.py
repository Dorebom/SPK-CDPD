"""Actual local one-shot child controls; every child prohibits browser/network."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cdpd_chatgpt_profile.process_runner import ProcessWorkerRunner
from cdpd_chatgpt_profile.worker import WorkerRequest
from test_capture_worker import Permit, request_data, observe

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/l06_process_fixture.py"


class FixtureRunner(ProcessWorkerRunner):
    def __init__(self, mode="normal"):
        super().__init__()
        self.mode = mode

    def _command(self):
        return [sys.executable, "-B", str(FIXTURE), self.mode]


@pytest.fixture(autouse=True)
def child_import_paths(monkeypatch):
    paths = [str(ROOT / "code"), str(ROOT / "tests")]
    paths.extend(str(p) for p in (ROOT / "packages").glob("*/src"))
    paths.extend(p for p in sys.path if p and ("python_deps" in p or "site-packages" in p))
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def run(mode="normal", data=None, permit=None):
    runner = FixtureRunner(mode)
    permit = Permit() if permit is None else permit
    data = request_data() if data is None else data
    result = runner(WorkerRequest.from_dict(data), permit=permit)
    return result, runner.last_process_metadata, permit


def record(request, result, metadata, *, expected, cap=("CAP-01",)):
    actual = {"capture_status": result["capture_status"], "failure_code": result["failure_code"],
              "structure_present": result["structure"] is not None, "worker_termination": result["worker_termination"],
              "process": metadata}
    observe(request, expected=expected, actual=actual, ac=("AC01", "AC03"), cap=cap, result=result)
    request.node._l06_evidence["commands"] = [metadata]
    request.node._l04_evidence["commands"] = [metadata]


def test_real_child_uses_exact_owned_pid_one_shot_and_no_files(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)
    result, metadata, permit = run()
    assert result["capture_status"] == "COMPLETE", metadata
    assert result["pid"] == metadata["pid"] and result["pid"] != os.getpid()
    assert metadata["child_exit_code"] == 0 and metadata["termination"] == "NORMAL"
    assert metadata["spawn_count"] == 1 and metadata["permit_checks"] == len(permit.challenges)
    assert len(permit.challenges) > 3 and result["command_audit"].count("Runtime.evaluate") == 1
    assert not list(tmp_path.iterdir())
    record(request, result, metadata, expected={"separate_exact_owned_pid": True, "spawn_count": 1, "files_written": 0, "capture_status": "COMPLETE"})


@pytest.mark.parametrize("mode,expected", [("truncated", "TRUNCATED"), ("capture_exception", "COLLECTOR_EXCEPTION"), ("epoch_after", "BROWSER_IDENTITY_MISMATCH")])
def test_child_real_worker_preserves_safe_partial_outcomes(mode, expected, request):
    result, metadata, _ = run(mode)
    assert result["capture_status"] == "PARTIAL" and result["failure_code"] == expected, metadata
    assert (result["structure"] is not None) == (mode == "truncated")
    assert metadata["child_exit_code"] == 0 and metadata["failure_reason"] == "NONE"
    assert "SYNTHETIC_SECRET_EXCEPTION" not in json.dumps(result)
    record(request, result, metadata, expected={"capture_status": "PARTIAL", "failure_code": expected, "structure_present": mode == "truncated"}, cap=("CAP-02", "CAP-04"))


def test_child_policy_rejection_has_no_permit_or_capture(request):
    result, metadata, permit = run(data=request_data(policy=False))
    assert result["capture_status"] == "REJECTED" and result["structure"] is None
    assert result["worker_termination"] == "NOT_STARTED" and not permit.challenges
    assert metadata["child_exit_code"] == 0
    record(request, result, metadata, expected={"capture_status": "REJECTED", "permit_checks": 0, "structure_present": False})


@pytest.mark.parametrize("mode", ["wrong_pid", "wrong_capture_id", "extra_field", "unsafe_structure", "duplicate_result", "oversized"])
def test_untrusted_child_result_discarded_whole(mode, request):
    result, metadata, _ = run(mode)
    assert result["capture_status"] == "PARTIAL" and result["structure"] is None
    assert result["failure_code"] == "WORKER_FAILED_OR_DEADLINE"
    assert metadata["failure_reason"] != "NONE" and metadata["spawn_count"] == 1
    assert metadata["termination"] in {"KILLED", "EXITED_ERROR"}
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    assert result["pid"] == metadata["pid"]
    record(request, result, metadata, expected={"structure_present": False, "failure_code": "WORKER_FAILED_OR_DEADLINE", "spawn_count": 1}, cap=("CAP-04",))


@pytest.mark.parametrize("mode", ["wrong_challenge", "duplicate_nonce"])
def test_child_permit_identity_and_nonce_cannot_be_replayed(mode, request):
    result, metadata, permit = run(mode)
    assert result["structure"] is None and metadata["failure_reason"] == "PERMIT_INVALID"
    assert len(permit.challenges) == (0 if mode == "wrong_challenge" else 1)
    record(request, result, metadata, expected={"structure_present": False, "reason": "PERMIT_INVALID", "callback_count": 0 if mode == "wrong_challenge" else 1}, cap=("CAP-02",))


@pytest.mark.parametrize("mode", ["early_exit", "result_then_nonzero"])
def test_nonzero_exit_never_accepts_temporary_dom_or_stderr(mode, request):
    result, metadata, _ = run(mode)
    assert result["structure"] is None and metadata["child_exit_code"] == 73
    assert metadata["termination"] == "EXITED_ERROR" and metadata["spawn_count"] == 1
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result) + json.dumps(metadata)
    record(request, result, metadata, expected={"structure_present": False, "child_exit_code": 73, "stderr_forwarded": False}, cap=("CAP-04",))


@pytest.mark.parametrize("mode", ["hang", "result_then_hang"])
def test_total_deadline_rejects_even_previously_received_result(mode, request):
    timeout = 0.3 if mode == "hang" else 2.0
    result, metadata, _ = run(mode, data=request_data(timeout=timeout))
    assert result["structure"] is None and metadata["timed_out"]
    assert metadata["termination"] == "KILLED" and metadata["child_exit_code"] is not None
    assert metadata["elapsed_ms"] < (timeout + 1.5) * 1000
    record(request, result, metadata, expected={"structure_present": False, "termination": "KILLED", "deadline_includes_final_exit": True}, cap=("CAP-05",))


def test_slow_permit_callback_wait_is_bounded_and_late_result_discarded(request):
    def slow(challenge):
        time.sleep(2.0)
        return Permit()(challenge)
    started = time.monotonic()
    result, metadata, _ = run(permit=slow)
    assert time.monotonic() - started < 3.0
    assert result["structure"] is None and metadata["failure_reason"] == "PERMIT_TIMEOUT"
    assert metadata["permit_checks"] == 1 and metadata["spawn_count"] == 1
    record(request, result, metadata, expected={"structure_present": False, "permit_timeout_seconds": 1.0, "late_authority_accepted": False}, cap=("CAP-05",))


@pytest.mark.parametrize("mode", ["wrong_response", "callback_exception", "final_revoke"])
def test_parent_permit_failure_cannot_grant_or_retain_dom(mode, request):
    def permit(challenge):
        if mode == "callback_exception":
            raise RuntimeError("SYNTHETIC_PRIVATE_PERMIT_EXCEPTION")
        response = Permit()(challenge)
        if mode == "wrong_response":
            response["nonce"] = "0" * 32
        if mode == "final_revoke" and challenge["phase"] == "FINAL":
            response.update(allowed=False, reason_code="BINDING_REVOKED")
        return response
    result, metadata, _ = run(permit=permit)
    assert result["structure"] is None
    expected = {"wrong_response": "PERMIT_INVALID", "callback_exception": "PERMIT_CALLBACK_FAILED", "final_revoke": "NONE"}[mode]
    assert metadata["failure_reason"] == expected
    if mode == "final_revoke":
        assert result["failure_code"] == "BINDING_REVOKED" and metadata["child_exit_code"] == 0
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result) + json.dumps(metadata)
    record(request, result, metadata, expected={"structure_present": False, "process_reason": expected, "raw_exception_forwarded": False}, cap=("CAP-02",))


@pytest.mark.parametrize("payload", [b'{"bad":"SYNTHETIC_PRIVATE_INPUT"}\n', b'X' * 131074], ids=["invalid_shape", "oversized"])
def test_child_rejects_malformed_or_excess_initial_request_without_echo(payload, request):
    command = [sys.executable, "-B", str(FIXTURE), "normal"]
    process = subprocess.run(command, input=payload, capture_output=True, timeout=5)
    assert process.returncode == 2 and not process.stderr
    message = json.loads(process.stdout)
    assert set(message) == {"schema", "code", "pid"}
    assert message["schema"] == "cdpd.capture-worker-protocol-error/v1"
    assert "SYNTHETIC_PRIVATE" not in process.stdout.decode()
    observe(request, expected={"exit_code": 2, "input_echoed": False},
            actual={"exit_code": process.returncode, "stdout_bytes": len(process.stdout), "safe_message": message}, ac=("AC01",), cap=("CAP-01",))


def test_spawn_time_is_part_of_total_capture_deadline(monkeypatch, request):
    import cdpd_chatgpt_profile.process_runner as module
    original = module.subprocess.Popen
    def delayed(*args, **kwargs):
        process = original(*args, **kwargs)
        time.sleep(0.2)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", delayed)
    result, metadata, permit = run(data=request_data(timeout=0.1))
    assert result["structure"] is None and metadata["timed_out"]
    assert not permit.challenges and metadata["spawn_count"] == 1
    assert metadata["termination"] == "KILLED"
    record(request, result, metadata, expected={"deadline_started_before_spawn": True, "permit_checks": 0, "structure_present": False}, cap=("CAP-05",))


def test_runner_terminates_only_owned_worker_and_preserves_other_owned_control(request):
    sentinel = subprocess.Popen([sys.executable, "-B", str(FIXTURE), "hang"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result, metadata, _ = run("hang", data=request_data(timeout=0.3))
        assert result["structure"] is None and metadata["termination"] == "KILLED"
        assert metadata["pid"] != sentinel.pid and sentinel.poll() is None
        record(request, result, metadata, expected={"only_owned_worker_terminated": True, "other_control_pid_alive": True}, cap=("CAP-05",))
    finally:
        sentinel.kill()
        sentinel.wait(timeout=2)


def test_production_cli_has_no_fixture_or_origin_override_flags(request):
    assert ProcessWorkerRunner()._command() == [sys.executable, "-B", "-m", "cdpd_chatgpt_profile.worker_cli"]
    command = [sys.executable, "-B", str(FIXTURE), "production_flags"]
    process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    assert process.returncode == 64 and not process.stdout and not process.stderr
    observe(request, expected={"exit_code": 64, "fixture_flag_accepted": False},
            actual={"exit_code": process.returncode, "stdout_bytes": len(process.stdout), "stderr_bytes": len(process.stderr)}, ac=("AC01",), cap=("CAP-01",))
