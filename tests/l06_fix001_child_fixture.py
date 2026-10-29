"""FIX001 local child instrumentation; never reachable through CLI flags.

The production CLI, Worker, request/permit protocol and result serialization run
unchanged. Only the external browser connection is replaced with deterministic
synthetic metadata. No browser, socket, additional child or real profile exists.
"""
from __future__ import annotations

import importlib.abc
import os
import socket
import subprocess
import sys
import time

from fix008_child_phase import milestone, instrument_fixture_worker
milestone("fixture_start")


def denied(*args, **kwargs):
    raise AssertionError("L06_FIX001_CHILD_NETWORK_BROWSER_PROCESS_PROHIBITED")


# Prohibit external effects before importing any product or test module.
for method in ("connect", "connect_ex", "bind", "listen", "accept", "sendto"):
    setattr(socket.socket, method, denied)
socket.create_connection = denied
socket.create_server = denied
subprocess.Popen = denied
os.system = denied
for method in ("posix_spawn", "posix_spawnp"):
    if hasattr(os, method):
        setattr(os, method, denied)


class NoBrowserImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"playwright", "selenium", "pyppeteer"}:
            raise ImportError("L06_FIX001_CHILD_BROWSER_IMPORT_PROHIBITED")
        return None


sys.meta_path.insert(0, NoBrowserImports())

from cdpd.collector import CaptureError
milestone("product_import_begin")
from cdpd_chatgpt_profile import worker_cli
milestone("product_import_end")
instrument_fixture_worker(worker_cli)
from l06_synthetic_connection import Connection, ORIGIN, TARGET

MODE = sys.argv[1] if len(sys.argv) == 2 else "INVALID"
BASE_MODE, _, FAULT = MODE.partition("+")
CREATED = []
first_nonce = None
original_write = worker_cli._write_line


def injected_write(fd, value, *, maximum, deadline):
    """Corrupt only the chosen synthetic child pipe output, never product code."""
    global first_nonce
    if value.get("schema") == "cdpd.capture-worker-permit-check/v1":
        milestone("first_challenge_write")
        value = dict(value)
        terminal = value.get("phase", "").startswith("TERMINAL:")
        if not terminal and first_nonce is None:
            first_nonce = value["nonce"]
        if terminal:
            if FAULT == "no_notice_abrupt_death":
                # Actual Worker detected a violation, but no corresponding
                # notice/result bytes reach its parent. The parent must not
                # invent the absent exact cause from this test's mode name.
                os._exit(74)
            if FAULT in {"no_notice", "no_notice_hang"}:
                # The child subsequently returns its valid original failure
                # result after its notification wait expires. No notice bytes
                # or authority callback reach the parent.
                return
            if FAULT == "terminal_wrong_capture":
                value["capture_id"] = "C-" + "a" * 32
            elif FAULT == "terminal_wrong_binding":
                value["binding_id"] = "33333333-3333-4333-8333-333333333333"
            elif FAULT == "terminal_wrong_hash":
                value["binding_sha256"] = "0" * 64
            elif FAULT == "terminal_wrong_generation":
                value["control_generation"] += 1
            elif FAULT == "terminal_generation_bool":
                value["control_generation"] = True
            elif FAULT == "terminal_wrong_supervisor":
                value["supervisor_instance_id"] = "44444444-4444-4444-8444-444444444444"
            elif FAULT == "terminal_duplicate_nonce":
                value["nonce"] = first_nonce
            elif FAULT == "terminal_duplicate_notice":
                original_write(fd, value, maximum=maximum, deadline=deadline)
    if value.get("schema") == "cdpd.capture-worker-result/v1":
        if FAULT == "result_wrong_capture":
            value = dict(value, capture_id="C-" + "b" * 32)
        elif FAULT == "no_notice_hang":
            # The notification was suppressed above. Withhold this result too,
            # until the parent terminates this owned process at its deadline.
            time.sleep(30)
            return
    original_write(fd, value, maximum=maximum, deadline=deadline)


worker_cli._write_line = injected_write


class ObservedConnection(Connection):
    """Assert cleanup belongs to the attached session; no trace-file side effect."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed_sessions = []

    def close(self, session=None):
        self.closed_sessions.append(session)
        assert session in {None, "SYNTHETIC-SESSION-A"}
        if BASE_MODE == "normal_detach":
            assert session == "SYNTHETIC-SESSION-A"
            self.send("Target.detachFromTarget", {"sessionId": session})
        super().close(session)


def change(connection):
    kind = BASE_MODE.rsplit("_", 1)[0]
    if kind == "pages":
        connection.extra_targets = [{"targetId": "FIX001-SECOND-TARGET", "type": "page",
                                     "url": ORIGIN + "/"}]
    elif kind == "origin":
        connection.url = "https://example.invalid/synthetic-only"
    elif kind == "epoch":
        connection.browser_id = "f" * 32
    elif kind == "target":
        connection.target_id = "FIX001-REPLACEMENT-TARGET"
    elif kind == "navigation":
        connection.loader_id = "SYNTHETIC-LOADER-B"
        connection.url = ORIGIN + "/synthetic-next-document"
    elif kind == "disconnect":
        connection.disconnected = True
    else:
        raise AssertionError("UNRECOGNIZED_SYNTHETIC_MODE")


def factory(endpoint, timeout):
    assert endpoint == "http://127.0.0.1:9222" and timeout > 0
    if BASE_MODE == "preconnect_failure":
        raise CaptureError("CDP_CONNECT_FAILED")
    if BASE_MODE in {"normal", "normal_detach"}:
        connection = ObservedConnection()
    elif BASE_MODE == "collector_exception":
        connection = ObservedConnection(fail_method="Runtime.evaluate")
    elif BASE_MODE.endswith("_before"):
        connection = ObservedConnection(before=change)
    elif BASE_MODE.endswith("_after"):
        connection = ObservedConnection(after=change)
    else:
        raise AssertionError("UNRECOGNIZED_SYNTHETIC_MODE")
    CREATED.append(connection)
    return connection


milestone("fixture_serve_begin")
exit_code = worker_cli.serve_worker(connection_factory=factory)
milestone("fixture_serve_end")
if exit_code == 0:
    assert len(CREATED) == (0 if BASE_MODE == "preconnect_failure" else 1)
    for connection in CREATED:
        assert connection.closed and len(connection.closed_sessions) == 1
        attached = [row["target_id"] for row in connection.calls
                    if row["method"] == "Target.attachToTarget"]
        assert attached in ([], [TARGET])
        assert connection.audit.count("Runtime.evaluate") <= 1
        if BASE_MODE == "normal_detach":
            assert connection.audit[-1] == "Target.detachFromTarget"
            assert not connection.disconnected
if FAULT == "result_then_nonzero":
    os.write(2, b"SYNTHETIC_PRIVATE_CHILD_ERROR_MUST_NOT_ESCAPE\n")
    exit_code = 73
elif FAULT == "result_then_hang":
    # The production parent must terminate this exact owned PID at its total
    # deadline. This sleep is never allowed to finish in the passing control.
    time.sleep(30)
raise SystemExit(exit_code)
