# FIX012 — Explicit WD04 input coordination

Authority: CANDIDATE_ONLY. Revision kind: test/fixture correction.

## Problem and evidence

FIX011's Linux full run recorded 1506 PASS, 1 FAIL and 12 NOT_RUN. WD04 stopped
at publication of the foreign-generation event, before its final assertions.
The original failure's errno and lock owner were not recorded and remain
unknown. FIX012 preserves that failure and the complete FIX011 source lineage.

Accepted L05 FileMailbox publication deliberately uses a non-waiting exclusive
quota lock. The journal writer holds the same lock across a durable append.
WD04's previous publication burst had no scheduling precondition separating it
from that writer. A separate controlled observation reproduced real EAGAIN
while the known healthy Supervisor writer held the lock, then confirmed that
the foreign generation is rejected after publication succeeds. A single
instrumented original WD04 pass does not remove this race or replace the old
full result.

WD04's purpose is duplicate/scope admission and one capture per operation.
Its test inputs must reach the actual mailbox before those assertions can be
assessed. FIX012 corrects that test precondition; it adds no product delivery
guarantee under lock contention.

## Explicit test-only coordination

Only WD04 opts into the independent fixture's `--publication-barrier` flag.
The fixture-local JournalSpool subclass parks at `process_pending` entry,
before entering the original accepted method or acquiring any quota lock.
After release, the original method is called normally. The product modules
and their OS lock, fsync, receipt and admission logic are byte identical.

The parent requests a pause without blocking the fixture monitor/RPC loop.
A unique token is acknowledged only once the writer is actually parked.
WD04 establishes three publication intervals: arm, initial failure, and the
four post-result inputs. Every original envelope is published exactly once
with the original FileMailbox; the repeated failure retains the same event
identity. Releasing each interval permits normal writer ingestion before
the original progress or rejection checks continue.

The original five final expectations remain: one runner call, one result,
one claim, foreign-scope rejection, and living sentinels. The other ten
watchdog testcase bodies are unchanged. All 1519 test IDs are retained,
but the WD04 test body and its fixture are explicitly revised.

## Failure behavior and bounds

The test barrier has a separate absolute 3-second coordination deadline.
It does not pause any monotonic clock, extend a watchdog deadline or move a
product timeout. An expired barrier fails the test. Both writer-side expiry
and a late resume request set sticky failure; a late resume cannot win a
condition-lock scheduling race and hide the expiry.

Incorrect/stale tokens, repeated pause, and unrequested resume are rejected.
The context manager always requests release, including publication failure.
If release also fails, the primary publication error is preserved. Fixture
SHUTDOWN, stdin EOF and finally cleanup release a parked writer. No event is
retried, no exception is converted to PASS, and no receipt is synthesized.

An explicitly held external quota lock still makes one actual publication
fail with CURRENT_NOT_DURABLE. Separate regression evidence verifies this
contract and verifies real inbox-to-journal delivery after release.

## Revision and validation boundaries

Two L07 test/fixture files are modified and one test-only helper is added.
The full original test runner, product sources, accepted upstream contracts,
existing schemas, numeric deadlines and earlier supplementary tests remain
unchanged. Old metadata and the original two test files are preserved under
`evidence/fix012/` with before/after digests and a patch.

The 28 supplementary checks are outside the original pytest testpaths. They
are not added to the 1519-case full result. The final full run uses one fresh
execution copy and the original runner with no test selection or diagnostic
overlay. It is accurately labelled a FIX012 revised-fixture run.

FIX011's existing Windows result remains evidence for FIX011. FIX012 native
Windows full execution and real-browser/native product acceptance are NOT_RUN.
This correction grants no Finding Closure, formal Gate, L07 COMPLETE, Stable,
Release or L09 authorization.
