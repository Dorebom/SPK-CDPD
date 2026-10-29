"""L07 protocol and deadline controls; only local synthetic worker processes."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from cdpd_chatgpt_profile.watchdog_runner import WatchdogWorkerRunner
from cdpd_chatgpt_profile.worker import WorkerInputError, WorkerRequest
from test_capture_worker import Permit, request_data

ROOT = Path(__file__).resolve().parents[1]


class FixtureRunner(WatchdogWorkerRunner):
    def __init__(self, mode="normal", *, fixed=False, extra=(), **kwargs):
        super().__init__(**kwargs)
        self.mode, self.fixed, self.extra = mode, fixed, extra

    def _command(self):
        name = "l06_fix001_child_fixture.py" if self.fixed else "l06_process_fixture.py"
        if self.extra:
            name = "l07_runner_fixture.py"
        return [sys.executable, "-B", str(ROOT / "tests" / name), self.mode, *self.extra]


@pytest.fixture(autouse=True)
def child_import_paths(monkeypatch):
    paths = [str(ROOT / "code"), str(ROOT / "tests")]
    paths.extend(str(p) for p in (ROOT / "packages").glob("*/src"))
    paths.extend(p for p in sys.path if p and ("python_deps" in p or "site-packages" in p))
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def run(mode="normal", *, timeout=5.0, permit=None, fixed=False, **kwargs):
    runner = FixtureRunner(mode, fixed=fixed, **kwargs)
    result = runner(WorkerRequest.from_dict(request_data(timeout=timeout)),
                    permit=Permit() if permit is None else permit)
    return result, runner


def record(request, runner, result, expected):
    request.node._l07_evidence = {
        "expected": expected,
        "actual": {"capture_status": result["capture_status"],
                   "failure_code": result["failure_code"],
                   "structure_present": result["structure"] is not None,
                   "worker_termination": result["worker_termination"],
                   "process": deepcopy(runner.last_process_metadata)},
        "ac": ["AC03"], "wd": ["WD-03"],
        "commands": [deepcopy(runner.last_process_metadata)],
        "scope": "LOCAL_PROCESS_AND_SYNTHETIC_CONNECTION_NO_NATIVE_WINDOWS_ACCEPTANCE",
    }


def test_exact_accepted_protocol_success_and_owned_pid(request):
    permit = Permit()
    result, runner = run(permit=permit)
    m = runner.last_process_metadata
    assert result["capture_status"] == "COMPLETE", m
    assert result["pid"] == m["pid"] and result["pid"] != os.getpid()
    assert m["spawn_count"] == m["spawn_attempt_count"] == 1
    assert m["termination"] == "NORMAL" and m["termination_facts"]["confirmed"]
    assert m["termination_facts"]["active_processes"] == 0
    assert m["permit_checks"] == len(permit.challenges) > 3
    assert result["command_audit"].count("Runtime.evaluate") == 1
    assert not runner.termination_unconfirmed
    record(request, runner, result, {"capture_status": "COMPLETE", "one_owned_pid": True,
                                    "tree_termination_confirmed": True})


@pytest.mark.parametrize("mode,reason", [
    ("wrong_pid", "RESULT_INVALID"), ("wrong_capture_id", "RESULT_INVALID"),
    ("extra_field", "RESULT_INVALID"), ("unsafe_structure", "RESULT_INVALID"),
    ("duplicate_result", "OUTPUT_AFTER_RESULT"), ("oversized", "OUTPUT_TOO_LARGE"),
    ("wrong_challenge", "PERMIT_INVALID"), ("duplicate_nonce", "PERMIT_INVALID"),
])
def test_accepted_identity_nonce_and_result_rejections_preserved(mode, reason, request):
    permit = Permit()
    result, runner = run(mode, permit=permit)
    m = runner.last_process_metadata
    assert result["structure"] is None and result["capture_status"] == "PARTIAL"
    assert m["failure_reason"] == reason, m
    assert m["spawn_attempt_count"] == 1 and not m["termination_unconfirmed"]
    if mode in {"wrong_challenge", "duplicate_nonce"}:
        assert len(permit.challenges) == (0 if mode == "wrong_challenge" else 1)
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result) + json.dumps(m)
    record(request, runner, result, {"structure_present": False, "failure_reason": reason,
                                    "resend_count": 0})


@pytest.mark.parametrize("mode", ["hang", "result_then_hang"])
def test_total_deadline_discards_result_and_preserves_unrelated_processes(mode, request):
    sentinel = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    try:
        timeout = 0.3 if mode == "hang" else 1.5
        result, runner = run(mode, timeout=timeout)
        m = runner.last_process_metadata
        assert result["structure"] is None and m["timed_out"], m
        assert m["termination_facts"]["confirmed"] and not m["termination_unconfirmed"]
        assert m["elapsed_ms"] < (timeout + 2.5) * 1000
        assert m["deadline_overrun_ms"] <= 250
        assert sentinel.poll() is None and sentinel.pid != m["pid"]
        record(request, runner, result, {"structure_present": False, "deadline_overrun_ms_max": 250,
                                        "unrelated_process_alive": True})
    finally:
        sentinel.kill()
        sentinel.wait(timeout=2)


def test_blocked_request_writer_cannot_block_deadline_monitor(monkeypatch, request):
    import cdpd_chatgpt_profile.watchdog_runner as module
    release = threading.Event()
    entered = threading.Event()
    original = module._write_line
    def blocked(*args, **kwargs):
        entered.set()
        release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "_write_line", blocked)
    try:
        result, runner = run(timeout=0.3)
        m = runner.last_process_metadata
        assert entered.is_set() and result["structure"] is None
        assert m["timed_out"] and m["termination_facts"]["confirmed"]
        assert m["elapsed_ms"] < 1500 and m["deadline_overrun_ms"] <= 250
        before = deepcopy(m)
    finally:
        release.set()
    time.sleep(0.03)
    assert runner.last_process_metadata == before
    record(request, runner, result, {"blocked_writer_does_not_block_kill": True,
                                    "sealed_metadata_unchanged": True})


def test_blocked_terminal_prepare_preserves_authenticated_cause_and_kill(request):
    release = threading.Event()
    entered = threading.Event()
    class BlockingPrepare(Permit):
        def prepare(self, challenge):
            entered.set()
            release.wait(4)
    try:
        result, runner = run("epoch_after", fixed=True, timeout=1.0, permit=BlockingPrepare())
        m = runner.last_process_metadata
        assert entered.is_set(), m
        assert result["failure_code"] == "BROWSER_IDENTITY_MISMATCH"
        assert result["structure"] is None and m["timed_out"]
        assert m["terminal_notice"]["phase"] == "TERMINAL:BROWSER_IDENTITY_MISMATCH"
        assert "terminal_receipt" not in m
        assert m["termination_facts"]["confirmed"] and m["deadline_overrun_ms"] <= 250
        before = deepcopy(m)
    finally:
        release.set()
    time.sleep(0.03)
    assert runner.last_process_metadata == before
    record(request, runner, result, {"blocked_persistence_does_not_block_kill": True,
                                    "authenticated_first_cause_preserved": True,
                                    "unconfirmed_authority_not_upgraded": True})


def test_absent_terminal_notice_and_result_does_not_recover_cause(request):
    result, runner = run("epoch_after+no_notice_abrupt_death", fixed=True)
    m = runner.last_process_metadata
    assert result["failure_code"] == "WORKER_FAILED_OR_DEADLINE"
    assert result["structure"] is None and "terminal_notice" not in m
    assert m["child_exit_code"] == 74 and m["spawn_count"] == 1
    record(request, runner, result, {"first_cause_invented": False, "spawn_count": 1})


def test_invalid_terminal_ack_stays_unknown_and_preserves_cause(request):
    result, runner = run("epoch_after", fixed=True, permit=Permit())
    m = runner.last_process_metadata
    assert result["failure_code"] == "BROWSER_IDENTITY_MISMATCH" and result["structure"] is None
    assert m["terminal_receipt"]["status"] == "UNCONFIRMED"
    assert m["terminal_receipt"]["shared_state"] == "UNKNOWN"
    record(request, runner, result, {"original_failure": "BROWSER_IDENTITY_MISMATCH",
                                    "shared_state": "UNKNOWN"})


def test_stalled_os_spawn_is_unconfirmed_and_no_automatic_retry(request):
    from cdpd_chatgpt_profile.owned_process import OwnedWorker
    release = threading.Event()
    completed = threading.Event()
    observations = []
    def delayed(*args, **kwargs):
        release.wait(4)
        observations.append(kwargs["cancellation"].is_set())
        try:
            return OwnedWorker.spawn(*args, **kwargs)
        finally:
            completed.set()
    result, runner = run(timeout=0.2, process_factory=delayed)
    m = runner.last_process_metadata
    assert result["structure"] is None and m["termination_unconfirmed"]
    assert m["termination_failure_code"] == "WORKER_TERMINATION_UNCONFIRMED"
    assert m["spawn_attempt_count"] == 1 and m["spawn_count"] is None and m["pid"] is None
    assert m["elapsed_ms"] < 700
    before = deepcopy(m)
    release.set()
    assert completed.wait(2) and observations == [True]
    assert runner.last_process_metadata == before and runner.termination_unconfirmed
    again = runner(WorkerRequest.from_dict(request_data(timeout=0.2)), permit=Permit())
    assert again["structure"] is None and runner.last_process_metadata["spawn_count"] == 0
    assert runner.last_process_metadata["new_worker_prohibited"]
    # Preserve both original uncertainty and the explicit next-capture block.
    record(request, runner, again, {"late_spawn_rejected_before_resume": True,
                                  "subsequent_spawn_count": 0})
    request.node._l07_evidence["original_process"] = before


def test_stalled_cleanup_is_bounded_sticky_and_metadata_never_upgraded(request):
    from cdpd_chatgpt_profile.owned_process import OwnedWorker
    release = threading.Event()
    finished = threading.Event()
    class BlockedCleanup:
        def __init__(self, process):
            self.process = process
        def __getattr__(self, name):
            return getattr(self.process, name)
        def terminate_and_confirm(self, *, timeout_s):
            facts = self.process.terminate_and_confirm(timeout_s=timeout_s)
            release.wait(4)
            finished.set()
            return facts
    def factory(*args, **kwargs):
        return BlockedCleanup(OwnedWorker.spawn(*args, **kwargs))
    try:
        result, runner = run("hang", timeout=0.2, cleanup_timeout=0.15, process_factory=factory)
        m = runner.last_process_metadata
        assert result["structure"] is None and m["termination_unconfirmed"]
        assert m["elapsed_ms"] < 1000 and runner.termination_unconfirmed
        before = deepcopy(m)
    finally:
        release.set()
    assert finished.wait(2) and runner.last_process_metadata == before
    record(request, runner, result, {"cleanup_wait_bounded": True, "unknown_is_sticky": True})


def test_known_precreation_failure_is_not_a_fake_process_or_unknown(request):
    class Refused(RuntimeError):
        process_created = False
        termination_confirmed = True
    def refuse(*args, **kwargs):
        raise Refused("private exception must not be persisted")
    result, runner = run(process_factory=refuse)
    m = runner.last_process_metadata
    assert result["pid"] == 0 and result["worker_termination"] == "NOT_STARTED"
    assert m["spawn_count"] == 0 and m["spawn_attempt_count"] == 1
    assert not m["termination_unconfirmed"] and not runner.termination_unconfirmed
    assert "private exception" not in json.dumps(m)
    record(request, runner, result, {"known_no_process": True, "spawn_count": 0})


def test_surviving_descendant_rejects_even_valid_zero_exit_result(tmp_path, request):
    result, runner = run("descendant_after_result", extra=(str(tmp_path / "descendant.pid"),))
    m = runner.last_process_metadata
    assert (tmp_path / "descendant.pid").exists()
    assert result["structure"] is None and result["capture_status"] == "PARTIAL"
    assert m["termination_facts"]["requested"]
    assert m["failure_reason"] == "DESCENDANTS_AFTER_RESULT" or m["termination_unconfirmed"], m
    record(request, runner, result, {"leader_zero_exit_is_not_tree_exit": True,
                                    "structure_present": False})


def test_production_command_retains_exact_worker_entrypoint():
    assert WatchdogWorkerRunner()._command() == [sys.executable, "-B", "-m", "cdpd_chatgpt_profile.worker_cli"]


def test_native_error_type_is_a_fixed_identifier_not_a_message():
    from cdpd_chatgpt_profile.watchdog_runner import _termination_dict
    facts = {"requested": True, "confirmed": False, "reason": "WORKER_TERMINATION_UNCONFIRMED",
             "exit_code": None, "active_processes": None, "native_error": "PermissionError"}
    assert _termination_dict(facts)["native_error"] == "PermissionError"
    with pytest.raises(ValueError):
        _termination_dict({**facts, "native_error": "PRIVATE RAW MESSAGE"})


def test_explicit_cancel_targets_only_current_worker_and_keeps_partial(request):
    from cdpd_chatgpt_profile.owned_process import OwnedWorker
    spawned = threading.Event()
    processes = []
    output = []
    def factory(*args, **kwargs):
        proc = OwnedWorker.spawn(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        return proc
    runner = FixtureRunner("hang", process_factory=factory)
    def invoke():
        output.append(runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit()))
    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    assert spawned.wait(2)
    assert runner.cancel_active()
    thread.join(timeout=2.5)
    assert not thread.is_alive() and len(output) == 1
    result = output[0]
    assert result["capture_status"] == "PARTIAL" and result["structure"] is None
    # Even a cancellation racing with spawn publication retains exact-owned
    # cleanup. Its sealed result may conservatively remain unconfirmed.
    assert processes[0].termination_result is not None
    assert processes[0].termination_result.confirmed
    assert not runner.cancel_active()
    record(request, runner, result, {"explicit_cancel_completed": True,
                                    "only_current_worker_targeted": True,
                                    "structure_present": False})


@pytest.mark.parametrize("kwargs", [{"poll_interval": 0.251}, {"poll_interval": float("nan")},
                                   {"cleanup_timeout": 2.01}, {"cleanup_timeout": True}])
def test_monitor_cannot_relax_poll_or_cleanup_bounds(kwargs):
    with pytest.raises(ValueError):
        WatchdogWorkerRunner(**kwargs)
