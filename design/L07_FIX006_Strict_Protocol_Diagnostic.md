# L07 FIX006 — Strict owned-worker protocol diagnosis (Candidate only)

## Motivation / evidence
FIX005 Windows 11 CPython 3.12.14 exact-15-pins full suite: **1,354 PASS, 37 FAIL, 32 NOT_RUN**, 1,423 collected. Independent Review 003 attributes 33 failures to L06 child/process paths and four to L07 watchdog; basic fixture 3/3 only proves receipt/exit, not the parent’s strict authenticated RESULT acceptance.

## Boundaries
- No changes to **any** product `code/` byte, `_valid_result`, `_valid_response`, permit, PID, capture, COMMIT, watchdog, deadline or fail-closed contract.
- Add read-only test-only classifier `tests/l07_fix006_protocol_diagnostic.py`; it **calls the original strict validator** first and cannot affect accept/reject. The reason classifier is best effort, not an independent authorization decision.
- Add `tests/l07_fix006_strict_fixture_probe.py`: only allowlisted synthetic child fixtures. Capture separate outcomes for exact RESULT validity, permit challenge/ACK, final exit, Windows private Job Object containment, confirmed termination and deadline. Return only enum reasons/counts and bounded booleans. Do not persist raw pipe messages, DOM, raw stderr, PID or private file paths.
- All seven browser NOT_RUN and platform-specific justified skips remain subject to independent assessment.
- A diagnostic probe's execution / SUCCESS is not acceptance. **Any remaining full-suite FAIL remains FAIL**. No self-approval of Findings.

## Risk and needed next evidence
The shared root cause of the 37 Windows failures is not established from Review 003. Therefore FIX006 does **not** claim to repair them; native Windows local diagnostic results are required before deciding whether production code or synthetic fixture must be changed. Fixed full suite on a fresh Windows execution copy remains NOT_RUN for FIX006.

## External history
43 inherited exact external original containers plus six new exact external FIX005 Candidate/receipt/Windows Evidence/receipt/Review 003/receipt were independently compared by byte size and SHA-256, 49/49 PASS. Prior containers remain unchanged.
