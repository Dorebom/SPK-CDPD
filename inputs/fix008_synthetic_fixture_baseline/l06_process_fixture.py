"""Local child fixture only; not reachable through production Worker CLI flags."""
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
    raise AssertionError("L06_PROCESS_FIXTURE_NETWORK_BROWSER_PROCESS_PROHIBITED")


# Install prohibitions before importing any product module or fixture helper.
for method in ("connect", "connect_ex", "bind", "listen", "accept", "sendto"):
    setattr(socket.socket, method, denied)
socket.create_connection = denied
socket.create_server = denied
subprocess.Popen = denied
os.system = denied
if hasattr(os, "posix_spawn"):
    os.posix_spawn = denied
if hasattr(os, "posix_spawnp"):
    os.posix_spawnp = denied


class NoBrowserImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"playwright", "selenium", "pyppeteer"}:
            raise ImportError("L06_PROCESS_FIXTURE_BROWSER_IMPORT_PROHIBITED")
        return None


sys.meta_path.insert(0, NoBrowserImports())
MODE = sys.argv[1] if len(sys.argv) == 2 else "normal"
if MODE == "hang":
    time.sleep(30)
    raise SystemExit(0)
if MODE == "early_exit":
    os.write(2, b"SYNTHETIC_PRIVATE_EXCEPTION_MUST_NOT_ESCAPE\n")
    os._exit(73)

milestone("product_import_begin")
from cdpd_chatgpt_profile import worker_cli
milestone("product_import_end")
instrument_fixture_worker(worker_cli)
from test_capture_worker import Connection, valid_structure

if MODE == "production_flags":
    sys.argv = ["cdpd_chatgpt_profile.worker_cli", "--fixture", "normal"]
    raise SystemExit(worker_cli.main())

original_write = worker_cli._write_line
first_nonce = None


def mutate_output(fd, value, *, maximum, deadline):
    global first_nonce
    if value.get("schema") == "cdpd.capture-worker-permit-check/v1":
        milestone("first_challenge_write")
        value = dict(value)
        if MODE == "wrong_challenge":
            value["binding_sha256"] = "0" * 64
        elif MODE == "duplicate_nonce":
            if first_nonce is None:
                first_nonce = value["nonce"]
            value["nonce"] = first_nonce
    if value.get("schema") == "cdpd.capture-worker-result/v1":
        value = dict(value)
        if MODE == "wrong_pid":
            value["pid"] += 1
        elif MODE == "wrong_capture_id":
            value["capture_id"] = "C-" + "a" * 32
        elif MODE == "extra_field":
            value["exception_message"] = "SYNTHETIC_PRIVATE_EXCEPTION_MUST_NOT_ESCAPE"
        elif MODE == "unsafe_structure":
            value["structure"]["nodes"][0]["textContent"] = "SYNTHETIC_PRIVATE_DOM_MUST_NOT_ESCAPE"
        elif MODE == "oversized":
            os.write(fd, b"X" * (worker_cli.MAX_RESULT_BYTES + 2))
            return
        elif MODE == "duplicate_result":
            original_write(fd, value, maximum=maximum, deadline=deadline)
    original_write(fd, value, maximum=maximum, deadline=deadline)


worker_cli._write_line = mutate_output


def factory(endpoint, timeout):
    assert endpoint == "http://127.0.0.1:9222"
    assert timeout > 0
    if MODE == "truncated":
        return Connection(structure=valid_structure(truncated=True))
    if MODE == "capture_exception":
        return Connection(fail_method="Runtime.evaluate")
    if MODE == "epoch_after":
        def change(c):
            c.browser_id = "a" * 32
        return Connection(after=change)
    return Connection()


milestone("fixture_serve_begin")
exit_code = worker_cli.serve_worker(connection_factory=factory)
milestone("fixture_serve_end")
if MODE == "result_then_hang":
    time.sleep(30)
if MODE == "result_then_nonzero":
    os.write(2, b"SYNTHETIC_PRIVATE_EXCEPTION_MUST_NOT_ESCAPE\n")
    exit_code = 73
raise SystemExit(exit_code)
