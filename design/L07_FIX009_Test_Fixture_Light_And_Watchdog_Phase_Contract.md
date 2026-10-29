# CDPD-L07 FIX009 — Synthetic Fixture and Watchdog Phase Design

Authority: **Candidate only**. No changes to product code, Worker protocol, PID validation, Permit, timeout ceilings, Job Object, Binding fail-closed, accepted assessments or earlier Candidate archives.

## Two test-only fixture replacements

`tests/l06_process_fixture.py` and `tests/l06_fix001_child_fixture.py` now import a source-exact local `Connection` and `valid_structure` from `tests/l06_synthetic_connection.py`. Its eight AST definition blocks are extracted byte-for-byte from FIX008 `tests/test_capture_worker.py`, but pytest, other test modules, browser and network imports are absent. FIX008 originals remain in `inputs/fix008_synthetic_fixture_baseline/` for independent comparison.

Source AST equality and 55 additional behavior/negative tests compare the original and lightweight implementations through the **same** real ProcessWorkerRunner and genuine strict challenge/result validation. Includes wrong target, browser epoch, origin, extra page, truncated structure, collector error, nonce replay, wrong binding/hash/generation/supervisor, terminal variants, result modification, abnormal exit/hang and cleanup. Successful local Linux tests are not Windows acceptance.

## Watchdog 1-second test preconditions

A new independent cold-start 1.0s no-notice test asserts fail-closed, no invented terminal authority, Job termination confirmation and bounded overrun. It does **not** claim a blocked terminal prepare was entered. The existing blocked-prepare 1s test remains unchanged; Windows failure remains unresolved until a valid authenticated terminal notice is delivered before deadline.

## Diagnostic V4 (separate package)

An external toolkit overlays **only** copies of the two test fixture files and installs a scoped `sys.setprofile` / pytest parent observer. Parent/child phases cover import, request pipe read, strict WorkerRequest validation, spool/event validation, capture_rule, run_capture_worker entry, Permit Gate first check, bounded pipe write and first challenge emission. Only fixed allowlisted function labels and monotonic timestamps are retained; no PID/path/nonce/IPC/DOM/stdout/stderr. Output is diagnostically instrumented and must not be used for formal timeout pass/fail. The instrumentation never runs in production Worker.

## Safety and acceptance limits

Do not weaken any 1/2/3-second deadline, PID auth or Job ownership. Original 1,462 IDs preserved, 57 new IDs added. One fresh Windows 11 CPython 3.12.14 + 15 exact distributions unselected 1,519-ID suite and separate independent assessment are required before any L07 completion or findings closure.
