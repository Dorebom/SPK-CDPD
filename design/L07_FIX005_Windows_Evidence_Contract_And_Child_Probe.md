# FIX005 Windows evidence-contract correction and synthetic child diagnosis

## R3 staging-fsync injection

The FIX004 inherited R3 assessment probe selected `.COMMIT.staged` by `/proc/self/fd/{fd}`, absent on native Windows. FIX005 scopes `storemod._write_new` to that exact filename and marks the executing thread with a lexical test-only flag. `os.fsync` is held only while the flagged `_write_new` call is inside its staged-file writer; payload/manifest/directory fsync are unaffected. The original assertions (INVALIDATE bounded timing, no COMMIT after denial) remain unchanged. The injection is a **test-only seam**, not a new runtime backdoor.

## Windows storage authorizer test scope

The original `[store_authorizer_fallback]` expected the same authorizer to be called exactly once for `state_dir`. FIX004 production now re-authorizes `store.root` and each bundle path: that legacy test callback wrongly rejected valid paths. FIX005 preserves the state-directory observation and separately grants only descendants of the test's `store.root`, in the test callback alone. Out-of-scope paths fail closed. `conftest.py` synthetic `tmp_path` scoped fixture remains unchanged; the production NTFS/ACL/reparse policy and evidence-store implementation remain unchanged.

## Windows subprocess: unproven root of 38 failures

FIX004 native Windows Evidence has 38 failures with `RESULT_INVALID`, nonzero fixture exit, and watchdog terminal outcomes. The root cause cannot be inferred safely from production-discarded stderr, so FIX005 does not weaken fail-closed worker protocol or Job Object ownership. REV001 ships a **separate, fixed synthetic-fixture-only** diagnostic which sends a valid synthetic worker request, handles permit challenges and captures only sanitized exception class/source module basenames and stderr digests. It cannot invoke browser, SHIRABE, arbitrary user commands or real workloads. The probe is not native product qualification. A Windows execution is necessary to identify and fix those 38 failures in a subsequent revision if they persist.

## Explicit limits

Synthetic test PASS is never Win32 directory power-loss durability, kernel handle isolation, actual NTFS ACL/elevation, real Chrome/SHIRABE or a Release test. The Candidate remains unapproved until fresh exact-pinned native full suite and independent Assessment.
