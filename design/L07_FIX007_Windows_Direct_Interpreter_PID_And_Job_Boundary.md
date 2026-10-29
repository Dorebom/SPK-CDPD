# FIX007 — Windows CPython venv redirector vs exact OS-owned process identity

## Observed fault / evidence
- User's same Windows CPython 3.12.14 venv probe gave `launcher_process_pid_equals_interpreter_os_getpid=false` twice (Chrome open and Chrome closed).
- FIX006 independent strict Windows diagnostic: 7/7 strict result rejection reason `PID` despite valid permit checks; previous pinned Windows full suite still 37 FAIL.
- CPython's Windows `venvlauncher.c` starts the base Python process; `CreateProcessW` PID may identify the redirector rather than the actual Worker.

## Candidate change
1. For native Windows CPython venv with exact version **3.12.14**, validate absolute existing `sys.executable`, `sys._base_executable`, venv `pyvenv.cfg`, Scripts/python.exe, base prefix. Do not accept caller-selected paths or substitute a different module.
2. If launching current Python, pass OS the base interpreter's full path and `__PYVENV_LAUNCHER__=venv/Scripts/python.exe`. This is an *internal, unsupported CPython path hint*, admitted ONLY for pinned 3.12.14. Other Windows venv versions or malformed installation fail closed.
3. Production Worker CLI receives expected venv prefix/exe/base paths in a private per-process environment, then checks the running Python's `sys.prefix`, `sys.executable`, `sys._base_executable`, version before processing request bytes. No fallback to non-venv base site-packages.
4. ProcessWorkerRunner retains `_valid_result(..., proc.pid)` and one real subprocess spawn. No self-asserted PID override or redirector descendant trust.
5. WatchdogRunner continues to call `OwnedWorker.spawn`. Its native Win32 backend creates **the actual base Python process suspended**, assigns the private Job atomically using PROC_THREAD_ATTRIBUTE_JOB_LIST, confirms exact membership before resume, and uses that returned process handle/PID for RESULT validation and active-processes-zero termination proof.
6. Inherited stale `__PYVENV_LAUNCHER__` and FIX007 attestation environment keys are removed before recomputation; `PYTHONHOME` is removed for this exact route.

## Known limitations and acceptance requirements
- `__PYVENV_LAUNCHER__` has no official stable API support; this is **candidate-only**, not general CPython Windows product support. See https://bugs.python.org/issue42041 and https://bugs.python.org/issue38905.
- Must confirm one direct real-interpreter PID, correct venv package resolution, private Job contains that PID and all descendants, cancellation/timeout truly kills the Job, and no unrelated processes are affected **on the user's real Windows machine**.
- Run FIX007 strict seven cases and *one fresh unfiltered* 1454-test full suite under Windows CPython 3.12.14 + exact 15 packages; preserve original Browser 7 NOT_RUN and platform-specific SKIPs. A failure stays FAIL; no aggregation of separate focused passes.
- Do not change the original 37 failure records or close L07-F-001/F-002 / L07 COMPLETE / Gate / Stable / Release / L09.
