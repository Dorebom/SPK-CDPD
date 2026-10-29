# FIX008 Decision — Windows Capture lock vs Fixture snapshot; bounded exit observation

Authority: CANDIDATE_ONLY. Baseline: FIX007 byte-pinned ZIP. Do not change accepted upstream models/packages or production EvidenceStore / WindowsStoragePolicy.

## Deterministic Capture issue

R9 V2 evidence observes `PermissionError` immediately after `FIXTURE_PRE_SNAPSHOT`, before ProcessWorkerRunner. The synthetic Fixture's `snapshot(tmp_path)` recursively opened `state/capture-writer.lock` while its own CaptureCoordinator held the exclusive first-byte OS lock. FIX008 changes only this **specific fixture comparison** to skip the exact relative `state/capture-writer.lock`. Lock ownership remains asserted with an independent `_FileLock` attempt during the child interval, lock bytes are checked after release, and every evidence, spool and other lock file remains in the invariance comparison. Product lock acquisition/release and fail-closed storage authorizations are unchanged.

Windows locked-file `read_bytes()` conflict itself is supported by the R9 trace plus code contract, but has not been directly reproduced in this Linux execution environment. Windows pinned retest is mandatory.

## EOF / process-exit order

In R9, stdout EOF precedes OS exit signalling. `proc.poll()` returned None and KILL was requested ~0.02ms later although exit code 73 was observed eventually. After an observed EOF, and only within the original absolute `capture_request.timeout_seconds` deadline, the parent waits at most 0.10s for natural exit. If still alive, it retains the original owned-child kill and bounded confirmation. The result is *always discarded* on any protocol failure. No late RESULT accepted, no authority, permit, deadline, or PID comparison loosened. Unsupported/non-EOF failures still kill immediately.

The 100ms budget is a bounded compatibility hypothesis, not Windows proof of deterministic exit observation. Timeout and failed confirmation remain fail-closed. No OS Job process supervision is changed.

## Other three failures / diagnostic boundary

Terminal Binding, Slow Permit, Watchdog Prepare still wait for the first authenticated challenge. Common root cause remains unknown. FIX008 adds **opt-in, test-fixture-only** phase emission (fixture start, product import begin/end, request read, first challenge write) to a sidecar outside the trusted IPC, and a diagnostic-only three-test runner; disabled during acceptance tests. It does not change product Worker schema, permission response, process spawn count, or trusted RESULT. Phase measurements are timing-invasive and cannot establish formal performance PASS.

## Gates

Previously failing 6 Windows tests require fresh noninstrumented Windows CPython 3.12.14 +15-pins retest; full suite collection must contain all 1,454 old IDs plus eight FIX008 new IDs. L07 Finding closure, COMPLETE, Gate/Stable/Release/L09 are NOT authorized by this Candidate.
