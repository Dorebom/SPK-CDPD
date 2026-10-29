"""Independent local Supervisor fixture; never imports a browser or uses CDP.

JSON stdin controls are test-only.  The Consumer/Chrome identities refer only
to sleep surrogates explicitly started by the parent test after READY.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.abc
import json
import os
from pathlib import Path
import queue
import socket
import sys
import threading
import time


def denied(*args, **kwargs):
    raise AssertionError("L07_FIXTURE_REAL_NETWORK_BROWSER_PROHIBITED")


for name in ("connect", "connect_ex", "bind", "listen", "accept", "sendto"):
    setattr(socket.socket, name, denied)
socket.create_connection = denied
socket.create_server = denied


class NoBrowserImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"playwright", "selenium", "pyppeteer"}:
            raise ImportError("L07_FIXTURE_BROWSER_IMPORT_PROHIBITED")
        return None


sys.meta_path.insert(0, NoBrowserImports())

from cdpd_chatgpt_profile.owned_process import RegisteredProcess, process_creation_identity
from cdpd_chatgpt_profile.supervisor import Supervisor, SupervisorConfig
import cdpd_chatgpt_profile.supervisor as supervisor_module
from cdpd_chatgpt_profile.spool import JournalSpool
from chatgpt_web_api.target_guard import BindingGuard, ClockSample
from test_capture_worker import bound_record
from test_l06_permit_authority import Authority
from test_l07_watchdog_runner import FixtureRunner
from l07_publication_barrier import PublicationBarrier


class RealtimeSyntheticClock:
    def __init__(self):
        self.started = time.monotonic()
        self.utc = datetime(2026, 10, 24, 0, 1, tzinfo=timezone.utc)

    def sample(self):
        now = time.monotonic()
        return ClockSample(self.utc + timedelta(seconds=now - self.started), now, 0)


class ModeAuthority(Authority):
    def set_mode(self, mode, *, binding_id=None, generation=None):
        if binding_id != self.current.binding_id or generation != self.current.generation:
            raise AssertionError("SYNTHETIC_AUTHORITY_SCOPE_MISMATCH")
        if mode not in {"DIAGNOSTIC_ONLY", "RECONCILIATION_REQUIRED"}:
            raise AssertionError("SYNTHETIC_AUTHORITY_MODE_INVALID")
        if self.current.mode != "RECONCILIATION_REQUIRED":
            self.current = replace(self.current, mode=mode)


class RecordingRunner(FixtureRunner):
    def __init__(self, mode, path, state_root):
        super().__init__(mode, poll_interval=0.02, cleanup_timeout=1.0)
        self.calls = []
        self.path = path
        self.state_root = state_root
        self.process_facts = []
        underlying = self.process_factory

        def observed_spawn(*args, **kwargs):
            process = underlying(*args, **kwargs)
            self.process_facts.append({"pid": process.pid,
                "creation_identity": process_creation_identity(process.pid),
                "ownership_scope": process.supervision_scope,
                "deadline_monotonic": process.deadline_monotonic})
            return process
        self.process_factory = observed_spawn

    def __call__(self, job, *, permit):
        value = job.to_dict()
        l06_s0 = self.state_root / "capture_state/incidents" / value["capture_id"] / "S0.json"
        sidecars = list((self.state_root / "operations").glob("*/L07_S0.json"))
        self.calls.append({"capture_id": value["capture_id"],
                           "operation_id": value["trigger_envelope"]["event"]["operation_id"],
                           "recorded_monotonic": time.monotonic(),
                           "l06_s0_present_before_runner": l06_s0.is_file(),
                           "l06_s0_sha256": hashlib.sha256(l06_s0.read_bytes()).hexdigest() if l06_s0.is_file() else None,
                           "consumer_s0_pins": [{"file": str(path),
                               "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sidecars]})
        with self.path.open("w", encoding="utf-8") as stream:
            json.dump(self.calls, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        return super().__call__(job, permit=permit)


def emit(request_id, **value):
    print(json.dumps({"id": request_id, **value}, allow_nan=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--locks", type=Path, required=True)
    parser.add_argument("--run-id", default="l07-synthetic-run")
    parser.add_argument("--worker-mode", default="normal")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--expiry", type=float, default=10.0)
    parser.add_argument("--publication-barrier", action="store_true")
    args = parser.parse_args()
    args.control.mkdir(parents=True, exist_ok=True)
    clock = RealtimeSyntheticClock()
    runner = RecordingRunner(args.worker_mode, args.control / "RUNNER_CALLS.json", args.root)
    block_writer, writer_blocked, release_writer = threading.Event(), threading.Event(), threading.Event()
    block_permit, permit_blocked, release_permit = threading.Event(), threading.Event(), threading.Event()
    permit_replies = []
    publication_barrier = PublicationBarrier() if args.publication_barrier else None
    if publication_barrier is not None:
        class PublicationControlledJournal(JournalSpool):
            def process_pending(self):
                # Test-only scheduling before the real accepted method takes
                # any quota lock. Every append/fsync/receipt remains original.
                publication_barrier.checkpoint()
                return super().process_pending()

        supervisor_module.JournalSpool = PublicationControlledJournal

    def fault(stage):
        if block_writer.is_set() and stage == "journal_before_fsync":
            writer_blocked.set()
            # Test-controlled only; monitor/status thread remains runnable.
            release_writer.wait(30.0)

    sup = Supervisor(SupervisorConfig(args.root, args.profile, args.run_id,
                     poll_seconds=0.02, heartbeat_expiry_seconds=args.expiry,
                     capture_timeout_seconds=args.timeout, lock_registry=args.locks),
                     runner=runner, utcnow=lambda: clock.sample().utc,
                     journal_fault_hook=fault)
    try:
        sup.start()
    except Exception as error:
        emit(0, status="BLOCKED", reason=getattr(error, "code", "FIXTURE_START_FAILED"))
        return 2

    emit(0, status="READY", pid=os.getpid(), supervisor_instance_id=sup.instance_id,
         scope="LOCAL_PROCESS_SYNTHETIC_ONLY", live_binding_count=0,
         started_monotonic=time.monotonic())
    commands = queue.Queue()

    def input_loop():
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except Exception:
                commands.put({"id": -1, "action": "INVALID"})
        commands.put({"id": -1, "action": "SHUTDOWN"})

    threading.Thread(target=input_loop, daemon=True).start()
    guard = authority = None
    try:
        while True:
            sup.tick()
            try:
                request = commands.get(timeout=0.01)
            except queue.Empty:
                continue
            request_id, action = request.get("id"), request.get("action")
            try:
                if action == "REGISTER":
                    record = bound_record()
                    authority = ModeAuthority(record, clock)
                    guard = BindingGuard(record, authority, clock=clock)
                    consumer = RegisteredProcess.open(request["consumer_pid"], request["consumer_identity"])
                    chrome = RegisteredProcess.open(request["chrome_pid"], request["chrome_identity"])
                    sup.register_binding(guard, producer_id="l07-synthetic-consumer",
                        producer_instance_id="l07-instance-A", consumer=consumer, chrome=chrome)
                    # Delay only a reply produced by the real accepted L04
                    # bridge. This test seam grants no synthetic permission.
                    for key, context in tuple(sup._contexts.items()):
                        bridge = context.permit
                        def delayed(challenge, bridge=bridge):
                            response = bridge(challenge)
                            if block_permit.is_set() and challenge["phase"] == "COORDINATOR_START":
                                permit_replies.append({"phase": challenge["phase"], "allowed": response["allowed"]})
                                permit_blocked.set()
                                release_permit.wait(10.0)
                            return response
                        sup._contexts[key] = replace(context, permit=delayed)
                    emit(request_id, status="REGISTERED", binding_id=record.binding_id,
                         binding_sha256=hashlib.sha256(record.to_bytes()).hexdigest(),
                         generation=guard.generation, utc=clock.sample().utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
                elif action == "STATUS":
                    active = runner._active
                    proc = None if active is None else active.proc
                    emit(request_id, status="STATUS", results=sup.results,
                         observations=sup.observations, rejections=sup.rejections,
                         worker_blocked=sup.worker_blocked, diagnostic_only=sup.diagnostic_only,
                         operation_count=len(sup._operations), runner_calls=runner.calls,
                         process_facts=runner.process_facts,
                         runner_metadata=runner.last_process_metadata,
                         active_worker_pid=None if proc is None else proc.pid,
                         writer_blocked=writer_blocked.is_set(),
                         writer_alive=sup._pump.thread.is_alive(),
                         writer_error=sup._pump.error,
                         permit_blocked=permit_blocked.is_set(), permit_replies=permit_replies,
                         stopped=sup._stop.is_set(),
                         task_alive=sup._task is not None and sup._task.is_alive(),
                         authority_mode=None if authority is None else authority.current.mode,
                         guard_state=None if guard is None else guard.state,
                         **({"publication_barrier": publication_barrier.snapshot()}
                            if publication_barrier is not None else {}),
                         utc=clock.sample().utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
                elif action == "PAUSE_PUBLICATION" and publication_barrier is not None:
                    token = publication_barrier.request_pause()
                    emit(request_id, status="PUBLICATION_PAUSE_REQUESTED", token=token)
                elif action == "RESUME_PUBLICATION" and publication_barrier is not None:
                    publication_barrier.resume(request.get("token"))
                    emit(request_id, status="PUBLICATION_RESUMED")
                elif action == "BLOCK_JOURNAL":
                    block_writer.set()
                    emit(request_id, status="ARMED")
                elif action == "RELEASE_JOURNAL":
                    block_writer.clear()
                    release_writer.set()
                    emit(request_id, status="RELEASED")
                elif action == "BLOCK_PERMIT":
                    block_permit.set()
                    emit(request_id, status="ARMED")
                elif action == "RELEASE_PERMIT":
                    block_permit.clear()
                    release_permit.set()
                    emit(request_id, status="RELEASED")
                elif action == "STOP":
                    sup.stop()
                    emit(request_id, status="STOPPED")
                elif action == "CLOSE_ATTEMPT":
                    try:
                        sup.close()
                        emit(request_id, status="CLOSED")
                    except Exception as error:
                        emit(request_id, status="BLOCKED", reason=getattr(error, "code", "FIXTURE_CLOSE_FAILED"))
                elif action == "SHUTDOWN":
                    if publication_barrier is not None:
                        publication_barrier.close()
                    release_writer.set()
                    release_permit.set()
                    sup.close()
                    emit(request_id, status="CLOSED")
                    return 0
                else:
                    emit(request_id, status="REJECTED")
            except Exception as error:
                emit(request_id, status="ERROR", reason=getattr(error, "code", "FIXTURE_OPERATION_FAILED"))
    finally:
        if publication_barrier is not None:
            publication_barrier.close()
        release_writer.set()
        release_permit.set()
        try:
            sup.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
