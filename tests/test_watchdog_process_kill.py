"""WD-01..05: real local processes, synthetic Consumer/Chrome and CDP data.

The independent Supervisor process is started *before* the two sleep surrogates.
No real Chrome/SHIRABE, browser library, socket or native Windows is exercised.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from cdpd_chatgpt_profile.evidence_store import verify_profile_bundle
from cdpd_chatgpt_profile.owned_process import RegisteredProcess, process_creation_identity
from cdpd_chatgpt_profile.spool import FileMailbox
from cdpd_chatgpt_profile.supervisor import Supervisor, SupervisorConfig, SupervisorError
from test_capture_worker import bound_record
from l07_publication_barrier import publication_batch


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
    reason="LOCAL_LINUX_PROCESS_SYNTHETIC_CONTROLS_NOT_NATIVE_WINDOWS_QUALIFICATION")


def environment():
    result = dict(os.environ)
    paths = [str(ROOT / "code"), str(ROOT / "tests")]
    paths.extend(str(path) for path in (ROOT / "packages").glob("*/src"))
    paths.extend(path for path in sys.path if path and ("site-packages" in path or "python_deps" in path))
    result["PYTHONPATH"] = os.pathsep.join(paths)
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


class IndependentSupervisor:
    def __init__(self, base, *, mode="normal", timeout=3.0, expiry=10.0,
                 root=None, profile=None, locks=None, expect_ready=True,
                 publication_barrier=False):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.root = Path(root) if root else self.base / "state"
        self.profile = Path(profile) if profile else self.base / "profile"
        self.profile.mkdir(parents=True, exist_ok=True)
        self.locks = Path(locks) if locks else self.base / "locks"
        self.control = self.base / "control"
        self.command = [sys.executable, "-B", str(ROOT / "tests/l07_supervisor_fixture.py"),
            "--root", str(self.root), "--profile", str(self.profile),
            "--control", str(self.control), "--locks", str(self.locks),
            "--worker-mode", mode, "--timeout", str(timeout), "--expiry", str(expiry)]
        if publication_barrier:
            self.command.append("--publication-barrier")
        self.process = subprocess.Popen(self.command, cwd=ROOT, env=environment(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True)
        self.process_identity = process_creation_identity(self.process.pid)
        self.incoming = queue.Queue()
        self.stderr = []
        self.transcript = []
        self.serial = 0

        def read_stdout():
            for line in self.process.stdout:
                try:
                    self.incoming.put(json.loads(line))
                except Exception:
                    self.incoming.put({"status": "INVALID_FIXTURE_OUTPUT"})
        def read_stderr():
            for line in self.process.stderr:
                self.stderr.append(line)
        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        self.ready = self._receive(0, timeout=6.0)
        if expect_ready:
            assert self.ready["status"] == "READY", self.ready

    def _receive(self, expected_id, *, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                response = self.incoming.get(timeout=min(0.1, end - time.monotonic()))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise AssertionError({"exit": self.process.returncode, "stderr": self.stderr})
                continue
            self.transcript.append({"response": response})
            assert response.get("id") == expected_id, response
            return response
        raise AssertionError({"fixture_timeout": expected_id, "stderr": self.stderr})

    def rpc(self, action, *, timeout=6.0, **fields):
        self.serial += 1
        value = {"id": self.serial, "action": action, **fields}
        self.transcript.append({"request": value})
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()
        return self._receive(self.serial, timeout=timeout)

    def status(self):
        result = self.rpc("STATUS")
        assert result["status"] == "STATUS", result
        return result

    def wait(self, predicate, *, timeout=8.0):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = self.status()
            if predicate(last):
                return last
            time.sleep(0.02)
        raise AssertionError({"predicate_timeout": last, "stderr": self.stderr})

    def close(self):
        if self.process.poll() is None:
            try:
                self.rpc("SHUTDOWN", timeout=5.0)
                self.process.wait(timeout=3.0)
            except Exception:
                self.process.kill()
                self.process.wait(timeout=3.0)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                stream.close()


def surrogate():
    return subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=environment())


@pytest.fixture
def setup_processes(tmp_path):
    created = []
    sentinels = []

    def make(*, name="case", mode="normal", timeout=3.0, expiry=10.0,
             publication_barrier=False):
        supervisor = IndependentSupervisor(tmp_path / name, mode=mode, timeout=timeout, expiry=expiry,
                                           publication_barrier=publication_barrier)
        created.append(supervisor)
        consumer, chrome = surrogate(), surrogate()
        sentinels.extend((consumer, chrome))
        registered = supervisor.rpc("REGISTER", consumer_pid=consumer.pid,
            consumer_identity=process_creation_identity(consumer.pid), chrome_pid=chrome.pid,
            chrome_identity=process_creation_identity(chrome.pid))
        assert registered["status"] == "REGISTERED", registered
        return supervisor, consumer, chrome, registered

    yield make, created
    for child in reversed(created):
        child.close()
    for process in sentinels:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3.0)


def event(supervisor, registration, sequence, *, kind="ACTION_STARTED", operation="operation-A",
          loss=0, exact=True, dispatch=None):
    choices = {"ACTION_STARTED": ("NONE", "OPERATION_ARMED", "NOT_DISPATCHED"),
               "ACTION_FAILED": ("TIMEOUT", "ACTION_TIMEOUT", "CONFIRMED"),
               "HEARTBEAT": ("NONE", "HEARTBEAT_SAMPLE", "UNKNOWN"),
               "ACTION_FINISHED": ("NONE", "OPERATION_FINISHED", "CONFIRMED")}
    failure, reason, selected_dispatch = choices[kind]
    return {
        "schema": "cdpd.chatgpt-event-envelope/v1", "event_id": str(uuid4()),
        "producer_seq": sequence, "occurred_at_utc": registration["utc"],
        "binding_id": registration["binding_id"], "binding_sha256": registration["binding_sha256"],
        "control_generation": registration["generation"],
        "event": {"schema": "cdpd.event/v1", "producer_id": "l07-synthetic-consumer",
                  "producer_instance_id": "l07-instance-A", "operation_id": operation,
                  "kind": kind, "failure_code": failure, "correlation": {"run_id": "l07-synthetic-run"}},
        "profile": {"reason": reason, "dispatch_state": dispatch or selected_dispatch,
                    "ui_context": "UNDETERMINED", "document_marker": None,
                    "loss_lower_bound": loss, "loss_count_exact": exact},
    }


def publish(supervisor, value):
    return FileMailbox(supervisor.root / "spool").publish(value)


def arm(supervisor, registration, *, loss=0, exact=True):
    value = event(supervisor, registration, 1, loss=loss, exact=exact)
    publish(supervisor, value)
    supervisor.wait(lambda state: state["operation_count"] == 1)
    return value


def checked_bundle(result):
    path = Path(result["bundle_path"])
    verification = verify_profile_bundle(path)
    assert verification["integrity_valid"] and verification["profile_valid"], verification
    return path, json.loads((path / "RESULT.json").read_text()), verification


def record(request, supervisor, *, expected, actual, ac=(), wd=(), extra=None):
    row = {"expected": expected, "actual": actual, "ac": list(ac), "wd": list(wd),
           "scope": "REAL_LOCAL_PROCESSES_SYNTHETIC_CONSUMER_CHROME_AND_CDP_ONLY",
           "commands": [supervisor.command], "supervisor_protocol": supervisor.transcript,
           "native_windows": "NOT_RUN", "supervisor_pid": supervisor.process.pid,
           "supervisor_creation_identity": supervisor.process_identity}
    contexts = []
    for path in (supervisor.root / "operations").glob("*/L07_S0.json"):
        raw = path.read_bytes()
        expected_pin = {"file": "L07_S0.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        actual_pin = json.loads((path.parent / "L07_S0_RECEIPT.json").read_text())
        assert actual_pin == expected_pin
        contexts.append(expected_pin)
    row["consumer_s0_pins"] = contexts
    if extra:
        row.update(extra)
    request.node._l07_evidence = row
    output = os.environ.get("CDPD_L07_EVIDENCE_DIR")
    if output:
        destination = Path(output) / ("case_" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "EXPECTED_ACTUAL.json").write_text(json.dumps(row, indent=2, allow_nan=False) + "\n")
        if supervisor.root.exists():
            shutil.copytree(supervisor.root, destination / "state", dirs_exist_ok=True)


def test_wd01_consumer_kill_keeps_independent_supervisor_and_s0(setup_processes, request):
    make, _ = setup_processes
    supervisor, consumer, chrome, registration = make()
    arm(supervisor, registration)
    consumer.kill(); consumer.wait(timeout=3.0)
    status = supervisor.wait(lambda value: bool(value["results"]))
    result = status["results"][0]
    path, bundle, verification = checked_bundle(result)
    actual = {"supervisor_alive": supervisor.process.poll() is None,
              "chrome_surrogate_alive": chrome.poll() is None,
              "consumer_exited": consumer.returncode is not None,
              "trigger_reason": result["trigger_reason"],
              "s0_exists": Path(result["s0_path"]).is_file(),
              "runner_calls": len(status["runner_calls"]),
              "semantic_valid": verification["profile_valid"]}
    expected = {"supervisor_alive": True, "chrome_surrogate_alive": True,
                "consumer_exited": True, "trigger_reason": "CONSUMER_EXITED",
                "s0_exists": True, "runner_calls": 1, "semantic_valid": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC01",), wd=("WD-01",),
           extra={"result": result, "capture_status": bundle["capture_status"]})
    assert actual == expected


def test_wd02_chrome_surrogate_kill_keeps_metadata_partial(setup_processes, request):
    make, _ = setup_processes
    supervisor, consumer, chrome, registration = make()
    arm(supervisor, registration)
    chrome.kill(); chrome.wait(timeout=3.0)
    status = supervisor.wait(lambda value: bool(value["results"]))
    result = status["results"][0]
    path, bundle, verification = checked_bundle(result)
    actual = {"supervisor_alive": supervisor.process.poll() is None,
              "consumer_surrogate_alive": consumer.poll() is None,
              "trigger_reason": result["trigger_reason"], "capture_status": bundle["capture_status"],
              "structure_present": (path / "STRUCTURE.json").exists(),
              "runner_calls": len(status["runner_calls"]),
              "s0_exists": Path(result["s0_path"]).is_file(),
              "semantic_valid": verification["profile_valid"]}
    expected = {"supervisor_alive": True, "consumer_surrogate_alive": True,
                "trigger_reason": "CHROME_EXITED", "capture_status": "PARTIAL",
                "structure_present": False, "runner_calls": 0,
                "s0_exists": True, "semantic_valid": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC02",), wd=("WD-02",), extra={"result": result})
    assert actual == expected


def test_wd03_worker_hang_stops_only_worker_and_preserves_s0(setup_processes, request):
    make, _ = setup_processes
    supervisor, consumer, chrome, registration = make(mode="hang", timeout=0.7)
    arm(supervisor, registration)
    publish(supervisor, event(supervisor, registration, 2, kind="ACTION_FAILED"))
    status = supervisor.wait(lambda value: bool(value["results"]))
    result = status["results"][0]
    path, bundle, verification = checked_bundle(result)
    metadata = result["process_metadata"]
    actual = {"consumer_alive": consumer.poll() is None, "chrome_alive": chrome.poll() is None,
              "supervisor_alive": supervisor.process.poll() is None,
              "capture_status": bundle["capture_status"], "structure_present": (path / "STRUCTURE.json").exists(),
              "one_worker": metadata["spawn_count"] == 1,
              "timed_out": metadata["timed_out"],
              "termination_confirmed": metadata["termination_facts"]["confirmed"],
              "bounded_decision": metadata["deadline_overrun_ms"] <= 350,
              "bounded_cleanup": metadata["elapsed_ms"] <= 2500,
              "s0_exists": Path(result["s0_path"]).is_file()}
    expected = {"consumer_alive": True, "chrome_alive": True, "supervisor_alive": True,
                "capture_status": "PARTIAL", "structure_present": False, "one_worker": True,
                "timed_out": True, "termination_confirmed": True, "bounded_decision": True,
                "bounded_cleanup": True, "s0_exists": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC03",), wd=("WD-03",), extra={"result": result})
    assert actual == expected


def test_wd04_duplicate_and_changed_producer_generation_cannot_recapture(setup_processes, request):
    make, _ = setup_processes
    supervisor, consumer, chrome, registration = make(publication_barrier=True)
    # WD04 tests duplicate/scope admission after actual publication. Accepted
    # L05 deliberately refuses a racing quota acquisition instead of waiting.
    # Establish each input batch at a confirmed writer poll boundary, publish
    # every original envelope once, then resume the real writer and monitor.
    with publication_batch(supervisor):
        publish(supervisor, event(supervisor, registration, 1))
    supervisor.wait(lambda state: state["operation_count"] == 1)
    failure = event(supervisor, registration, 2, kind="ACTION_FAILED")
    with publication_batch(supervisor):
        publish(supervisor, failure)
    status = supervisor.wait(lambda value: bool(value["results"]))
    with publication_batch(supervisor):
        publish(supervisor, failure)
        other = event(supervisor, registration, 3, kind="ACTION_FAILED")
        publish(supervisor, other)
        foreign_producer = event(supervisor, registration, 1, kind="ACTION_FAILED")
        foreign_producer["event"]["producer_id"] = "other-producer"
        foreign_producer["event"]["producer_instance_id"] = "other-producer-instance"
        publish(supervisor, foreign_producer)
        foreign_generation = event(supervisor, registration, 4, kind="ACTION_FAILED")
        foreign_generation["control_generation"] = 2
        publish(supervisor, foreign_generation)
    status = supervisor.wait(lambda value: value["rejections"] >= 2)
    actual = {"runner_calls": len(status["runner_calls"]), "results": len(status["results"]),
              "claims": len(list((supervisor.root / "operations").glob("*/CLAIM.json"))),
              "foreign_rejected": status["rejections"] >= 2,
              "sentinels_alive": consumer.poll() is None and chrome.poll() is None}
    expected = {"runner_calls": 1, "results": 1, "claims": 1, "foreign_rejected": True, "sentinels_alive": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC04",), wd=("WD-04",))
    assert actual == expected


def test_wd05_supervisor_crash_restart_recovers_old_s0_without_new_worker(setup_processes, request, tmp_path):
    make, created = setup_processes
    supervisor, consumer, chrome, registration = make(mode="hang", timeout=10.0)
    arm(supervisor, registration)
    publish(supervisor, event(supervisor, registration, 2, kind="ACTION_FAILED"))
    running = supervisor.wait(lambda value: value["active_worker_pid"] is not None)
    worker_pid = running["active_worker_pid"]
    worker_identity = next(fact["creation_identity"] for fact in running["process_facts"] if fact["pid"] == worker_pid)
    worker = RegisteredProcess.open(worker_pid, worker_identity)
    cleanup_fd = os.pidfd_open(worker_pid, 0)
    # Confirm both handles still name the known fixture Worker before retaining
    # a test-only cleanup capability. A reused numeric PID is never sufficient.
    cleanup_identity_confirmed = worker.is_alive() and process_creation_identity(worker_pid) == worker_identity
    def cleanup_owned_fixture():
        try:
            if cleanup_identity_confirmed and worker.is_alive():
                try:
                    signal.pidfd_send_signal(cleanup_fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            os.close(cleanup_fd)
            worker.close()
    request.addfinalizer(cleanup_owned_fixture)
    assert cleanup_identity_confirmed
    s0_files = list((supervisor.root / "capture_state/incidents").glob("*/S0.json"))
    assert len(s0_files) == 1
    before = hashlib.sha256(s0_files[0].read_bytes()).hexdigest()
    first_instance = supervisor.ready["supervisor_instance_id"]
    supervisor.process.kill(); supervisor.process.wait(timeout=3.0)
    end = time.monotonic() + 4.0
    while worker.is_alive() and time.monotonic() < end:
        time.sleep(0.02)
    orphan_stopped = not worker.is_alive()
    resumed = IndependentSupervisor(tmp_path / "restarted", root=supervisor.root,
        profile=supervisor.profile, locks=supervisor.locks, mode="normal")
    created.append(resumed)
    status = resumed.wait(lambda value: bool(value["results"]))
    result = status["results"][0]
    path, bundle, verification = checked_bundle(result)
    actual = {"old_worker_stopped": orphan_stopped,
              "new_instance": resumed.ready["supervisor_instance_id"] != first_instance,
              "recovery": result["recovery"], "runner_calls": len(status["runner_calls"]),
              "old_s0_unchanged": hashlib.sha256(s0_files[0].read_bytes()).hexdigest() == before,
              "capture_status": bundle["capture_status"], "structure_present": (path / "STRUCTURE.json").exists(),
              "sentinels_alive": consumer.poll() is None and chrome.poll() is None,
              "new_worker_admission_blocked": status["worker_blocked"]}
    expected = {"old_worker_stopped": True, "new_instance": True, "recovery": True, "runner_calls": 0,
                "old_s0_unchanged": True, "capture_status": "PARTIAL", "structure_present": False,
                "sentinels_alive": True, "new_worker_admission_blocked": True}
    record(request, resumed, expected=expected, actual=actual, ac=("AC01", "AC03", "AC04"), wd=("WD-05",),
           extra={"original_supervisor_command": supervisor.command, "original_protocol": supervisor.transcript,
                  "original_worker_pid": worker_pid, "original_worker_identity": worker_identity,
                  "old_s0_sha256": before, "result": result})
    assert actual == expected


def test_same_profile_second_supervisor_is_rejected(setup_processes, request, tmp_path):
    make, created = setup_processes
    supervisor, consumer, chrome, registration = make()
    other = IndependentSupervisor(tmp_path / "second", profile=supervisor.profile,
                                   locks=supervisor.locks, expect_ready=False)
    created.append(other)
    actual = {"second_status": other.ready["status"], "reason": other.ready.get("reason"),
              "first_alive": supervisor.process.poll() is None,
              "sentinels_alive": consumer.poll() is None and chrome.poll() is None}
    expected = {"second_status": "BLOCKED", "reason": "PROFILE_ALREADY_OWNED",
                "first_alive": True, "sentinels_alive": True}
    record(request, supervisor, expected=expected, actual=actual, wd=("WD-05",))
    assert actual == expected


def test_blocked_journal_fsync_does_not_block_worker_deadline_or_release_owner(setup_processes, request, tmp_path):
    make, created = setup_processes
    supervisor, consumer, chrome, registration = make(mode="hang", timeout=1.0)
    arm(supervisor, registration)
    publish(supervisor, event(supervisor, registration, 2, kind="ACTION_FAILED"))
    supervisor.wait(lambda value: value["active_worker_pid"] is not None)
    assert supervisor.rpc("BLOCK_JOURNAL")["status"] == "ARMED"
    publish(supervisor, event(supervisor, registration, 1, kind="HEARTBEAT"))
    supervisor.wait(lambda value: value["writer_blocked"])
    status = supervisor.wait(lambda value: bool(value["runner_metadata"].get("sealed")))
    closed = supervisor.rpc("CLOSE_ATTEMPT")
    other = IndependentSupervisor(tmp_path / "fsync-second", profile=supervisor.profile,
                                   locks=supervisor.locks, expect_ready=False)
    created.append(other)
    metadata = status["runner_metadata"]
    actual = {"writer_was_blocked": status["writer_blocked"],
              "worker_timeout": metadata["timed_out"],
              "worker_termination_confirmed": metadata["termination_facts"]["confirmed"],
              "bounded_decision": metadata["deadline_overrun_ms"] <= 350,
              "close_status": closed["status"], "close_reason": closed.get("reason"),
              "second_supervisor": other.ready.get("reason"),
              "sentinels_alive": consumer.poll() is None and chrome.poll() is None}
    expected = {"writer_was_blocked": True, "worker_timeout": True, "worker_termination_confirmed": True,
                "bounded_decision": True, "close_status": "BLOCKED",
                "close_reason": "SUPERVISOR_STOP_UNCONFIRMED",
                "second_supervisor": "PROFILE_ALREADY_OWNED", "sentinels_alive": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC03",), wd=("WD-03", "WD-05"),
           extra={"process_metadata": metadata})
    assert supervisor.rpc("RELEASE_JOURNAL")["status"] == "RELEASED"
    assert actual == expected


def test_startup_blocked_writer_does_not_release_profile_ownership(tmp_path, request):
    profile = tmp_path / "profile"
    profile.mkdir()
    locks = tmp_path / "locks"
    release, entered = threading.Event(), threading.Event()

    def fault(stage):
        if stage == "current_before_fsync":
            entered.set()
            release.wait(10.0)

    first = Supervisor(SupervisorConfig(tmp_path / "first", profile, "run", lock_registry=locks), journal_fault_hook=fault)
    second = Supervisor(SupervisorConfig(tmp_path / "second", profile, "run", lock_registry=locks))
    actual = {}
    try:
        with pytest.raises(SupervisorError, match="STARTUP_TIMEOUT"):
            first.start()
        assert entered.is_set()
        try:
            second.start()
        except SupervisorError as error:
            actual["second_reason"] = error.code
        else:
            actual["second_reason"] = "UNEXPECTEDLY_STARTED"
        actual["old_writer_alive"] = first._pump.thread.is_alive()
        expected = {"second_reason": "PROFILE_ALREADY_OWNED", "old_writer_alive": True}
        request.node._l07_evidence = {"expected": expected, "actual": actual,
            "ac": [], "wd": ["WD-05"], "scope": "LOCAL_THREAD_STORAGE_FAULT_NO_NATIVE_WINDOWS"}
        assert actual == expected
    finally:
        release.set()
        for item in (first, second):
            try:
                item.close()
            except Exception:
                pass


def test_original_consumer_unknown_loss_remains_in_l07_s0(setup_processes, request):
    make, _ = setup_processes
    supervisor, consumer, chrome, registration = make()
    arm(supervisor, registration, loss=4, exact=False)
    publish(supervisor, event(supervisor, registration, 2, kind="ACTION_FAILED", loss=4, exact=False))
    status = supervisor.wait(lambda value: bool(value["results"]))
    result = status["results"][0]
    path, bundle, verification = checked_bundle(result)
    sources = list((supervisor.root / "operations").glob("*/L07_S0.json"))
    assert len(sources) == 1
    source = json.loads(sources[0].read_text())

    def counters(value):
        if isinstance(value, dict):
            if "loss_lower_bound" in value and "loss_count_exact" in value:
                yield value["loss_lower_bound"], value["loss_count_exact"]
            for child in value.values():
                yield from counters(child)
        elif isinstance(value, list):
            for child in value:
                yield from counters(child)
    observed = list(counters(source))
    actual = {"original_unknown_loss_preserved": (4, False) in observed,
              "counter_not_reclassified_exact": all(exact is False for loss, exact in observed if loss >= 4),
              "capture_status": bundle["capture_status"], "semantic_valid": verification["profile_valid"],
              "s0_sidecar_present": len(sources) == 1}
    expected = {"original_unknown_loss_preserved": True, "counter_not_reclassified_exact": True,
                "capture_status": "PARTIAL", "semantic_valid": True, "s0_sidecar_present": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC01",),
           extra={"source_counters": observed, "source_s0_sha256": hashlib.sha256(sources[0].read_bytes()).hexdigest(),
                  "result": result})
    assert actual == expected


def test_brief_mailbox_quota_contention_keeps_writer_and_exactly_one_capture(setup_processes, request):
    make, _ = setup_processes
    supervisor, consumer, chrome, registration = make()
    arm(supervisor, registration)
    entered, release = threading.Event(), threading.Event()
    publication = {}
    failure = event(supervisor, registration, 2, kind="ACTION_FAILED")

    def fault(stage):
        if stage == "inbox_after_link":
            entered.set()
            release.wait(5.0)

    def publisher():
        try:
            publication["receipt"] = FileMailbox(supervisor.root / "spool", fault_hook=fault).publish(failure)
        except Exception as error:
            publication["error"] = getattr(error, "code", type(error).__name__)

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    try:
        assert entered.wait(2.0)
        # The immutable ready file exists while its publisher retains the
        # nonblocking L05 quota lock. Poll through several real Supervisor ticks.
        end = time.monotonic() + .2
        samples = []
        while time.monotonic() < end:
            samples.append(supervisor.status())
            time.sleep(.02)
    finally:
        release.set()
        thread.join(2.0)
    status = supervisor.wait(lambda value: bool(value["results"]))
    result = status["results"][0]
    _, _, verification = checked_bundle(result)
    actual = {"writer_alive_during_contention": all(value["writer_alive"] for value in samples),
              "no_storage_error_during_contention": all(value["writer_error"] is None for value in samples),
              "publisher_returned": not thread.is_alive(),
              "local_publication_only": publication.get("receipt", {}).get("stage") == "LOCAL_PUBLISHED",
              "runner_calls": len(status["runner_calls"]), "results": len(status["results"]),
              "semantic_valid": verification["profile_valid"],
              "sentinels_alive": consumer.poll() is None and chrome.poll() is None}
    expected = {"writer_alive_during_contention": True, "no_storage_error_during_contention": True,
                "publisher_returned": True, "local_publication_only": True,
                "runner_calls": 1, "results": 1, "semantic_valid": True, "sentinels_alive": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC04",), wd=("WD-04",),
           extra={"publication": publication, "contention_samples": samples})
    assert actual == expected


def test_late_healthy_l04_permit_after_stop_cannot_spawn_worker(setup_processes, request):
    make, _ = setup_processes
    supervisor, consumer, chrome, registration = make()
    arm(supervisor, registration)
    assert supervisor.rpc("BLOCK_PERMIT")["status"] == "ARMED"
    publish(supervisor, event(supervisor, registration, 2, kind="ACTION_FAILED"))
    pending = supervisor.wait(lambda value: value["permit_blocked"])
    assert pending["permit_replies"] == [{"phase": "COORDINATOR_START", "allowed": True}]
    assert supervisor.rpc("STOP")["status"] == "STOPPED"
    assert supervisor.rpc("RELEASE_PERMIT")["status"] == "RELEASED"
    status = supervisor.wait(lambda value: not value["task_alive"])
    # stop intentionally stops admission/tick collection; finalizer evidence
    # remains the durable result, without restarting the monitoring loop.
    paths = list((supervisor.root / "operations").glob("*/OUTCOME.json"))
    assert len(paths) == 1
    result = json.loads(paths[0].read_text())
    path, bundle, verification = checked_bundle(result)
    actual = {"stopped": status["stopped"], "runner_calls": len(status["runner_calls"]),
              "worker_processes": len(status["process_facts"]),
              "spawn_count": result["process_metadata"]["spawn_count"],
              "s0_exists": Path(result["s0_path"]).is_file(),
              "capture_status": bundle["capture_status"],
              "structure_present": (path / "STRUCTURE.json").is_file(),
              "semantic_valid": verification["profile_valid"],
              "sentinels_alive": consumer.poll() is None and chrome.poll() is None}
    expected = {"stopped": True, "runner_calls": 0, "worker_processes": 0, "spawn_count": 0,
                "s0_exists": True, "capture_status": "PARTIAL", "structure_present": False,
                "semantic_valid": True, "sentinels_alive": True}
    record(request, supervisor, expected=expected, actual=actual, ac=("AC03",), wd=("WD-03",),
           extra={"delayed_actual_l04_reply": pending["permit_replies"], "result": result})
    assert actual == expected
