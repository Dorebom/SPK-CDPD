"""FIX007 Windows owned-worker interpreter launch planning.

CPython 3.12.14's Windows venv ``Scripts/python.exe`` is a redirector.  It
creates the real interpreter as another process, breaking the L06/L07 exact
OS-owned PID / RESULT.pid contract.  On the *pinned* Windows version only,
launch the base interpreter directly with CPython's implementation-specific
``__PYVENV_LAUNCHER__`` path hint. The child must attest its venv path before
serving a real WorkerRequest. The Windows Job is assigned to the resulting
OS-created interpreter (not a redirector).

``__PYVENV_LAUNCHER__`` is NOT a stable public API: reject other CPython
versions rather than claim product support without a new native assessment.
No rebind, PID-self-report trust, shell, alternate Worker entry point, or
external spawn occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

_EXPECTED_VENV = "CDPD_L07_EXPECTED_VENV_PREFIX"
_EXPECTED_EXE = "CDPD_L07_EXPECTED_VENV_EXECUTABLE"
_EXPECTED_BASE = "CDPD_L07_EXPECTED_BASE_EXECUTABLE"
_MARKERS = (_EXPECTED_VENV, _EXPECTED_EXE, _EXPECTED_BASE)
_PINNED = (3, 12, 14)
_WORKER_MODULE = "cdpd_chatgpt_profile.worker_cli"


class WorkerInterpreterError(RuntimeError):
    """Fixed code only; no user-controlled path/exception in the error."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Runtime:
    platform: str
    implementation: str
    version: tuple[int, int, int]
    executable: str
    base_executable: str | None
    prefix: str
    base_prefix: str


def _runtime() -> _Runtime:
    return _Runtime(sys.platform, sys.implementation.name, tuple(sys.version_info[:3]),
                    sys.executable, getattr(sys, "_base_executable", None),
                    sys.prefix, sys.base_prefix)


def _path_eq(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _within(path: str, parent: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath((os.path.abspath(path), os.path.abspath(parent)))) == os.path.normcase(os.path.abspath(parent))
    except (ValueError, TypeError):
        return False


def _validate_direct_runtime(rt: _Runtime) -> None:
    if rt.implementation != "cpython" or rt.version != _PINNED:
        raise WorkerInterpreterError("UNSUPPORTED_WINDOWS_VENV_INTERPRETER")
    if not all(type(x) is str and os.path.isabs(x) for x in
               (rt.executable, rt.base_executable, rt.prefix, rt.base_prefix)):
        raise WorkerInterpreterError("UNTRUSTED_WINDOWS_VENV_PATH")
    if (_path_eq(rt.prefix, rt.base_prefix)
            or not _path_eq(rt.executable, os.path.join(rt.prefix, "Scripts", "python.exe"))
            or not _within(rt.base_executable, rt.base_prefix)
            or _path_eq(rt.executable, rt.base_executable)
            or not Path(rt.base_executable).is_file()
            or not Path(rt.executable).is_file()
            or not Path(rt.prefix, "pyvenv.cfg").is_file()):
        raise WorkerInterpreterError("UNTRUSTED_WINDOWS_VENV_PATH")


def plan_worker_launch(command: list[str], environment: dict[str, str]) -> tuple[list[str], dict[str, str], str]:
    """Return exact CreateProcess/Popen argv and environment for one owned worker.

    Pure planning: no extra process launch, no PID override. All other
    commands retain their original interpreter and package-resolution contract.
    The Windows venv redirector path is replaced *only* if it was exactly the
    current Python executable and the interpreter is the pinned CPython.
    """
    if not isinstance(command, (tuple, list)) or not command or any(type(x) is not str for x in command):
        raise WorkerInterpreterError("INVALID_WORKER_COMMAND")
    if type(environment) is not dict or any(type(k) is not str or type(v) is not str for k, v in environment.items()):
        raise WorkerInterpreterError("INVALID_WORKER_ENVIRONMENT")
    argv, env = list(command), environment.copy()
    rt = _runtime()
    if rt.platform != "win32":
        return argv, env, "UNCHANGED_NONWINDOWS"
    # A stale launcher hint inherited from an unrelated parent must never
    # redirect a newly spawned process or carry false venv provenance.
    env.pop("__PYVENV_LAUNCHER__", None)
    for key in _MARKERS:
        env.pop(key, None)
    if not _path_eq(argv[0], rt.executable):
        return argv, env, "UNCHANGED_CUSTOM_EXECUTABLE"
    if _path_eq(rt.prefix, rt.base_prefix) or (
            rt.base_executable and _path_eq(rt.executable, rt.base_executable)):
        return argv, env, "UNCHANGED_BASE_INTERPRETER"
    _validate_direct_runtime(rt)
    # In this CPython release the hint preserves sys.prefix / venv site
    # selection while the OS process returned by CreateProcess is the actual
    # interpreter. Never use PYTHONPATH to substitute venv package resolution.
    env.pop("PYTHONHOME", None)
    env["__PYVENV_LAUNCHER__"] = rt.executable
    if len(argv) >= 4 and argv[1:4] == ["-B", "-m", _WORKER_MODULE]:
        env[_EXPECTED_VENV] = rt.prefix
        env[_EXPECTED_EXE] = rt.executable
        env[_EXPECTED_BASE] = rt.base_executable
    argv[0] = rt.base_executable
    return argv, env, "WIN_CP31214_DIRECT_BASE_ATTESTED"


def worker_runtime_attested(environ: dict[str, str] | None = None) -> bool:
    """Fail closed before a WorkerRequest if the direct base lost its venv.

    A fixture command may not use worker_cli and cannot authenticate a real
    capture. The real production CLI checks this at entry. This is NOT a
    replacement for the parent's strict OS-owned RESULT PID validation.
    """
    env = os.environ if environ is None else environ
    present = [key in env for key in _MARKERS]
    if not any(present):
        return True  # legacy/manual entry point, parent RESULT PID still gates
    if not all(present) or any(type(env[key]) is not str or not os.path.isabs(env[key]) for key in _MARKERS):
        return False
    rt = _runtime()
    return (rt.platform == "win32" and rt.implementation == "cpython"
        and rt.version == _PINNED and _path_eq(rt.prefix, env[_EXPECTED_VENV])
        and _path_eq(rt.executable, env[_EXPECTED_EXE])
        and type(rt.base_executable) is str
        and _path_eq(rt.base_executable, env[_EXPECTED_BASE])
        and not _path_eq(rt.prefix, rt.base_prefix))
