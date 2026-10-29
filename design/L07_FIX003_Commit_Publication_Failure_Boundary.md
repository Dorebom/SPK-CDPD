# CDPD-L07 FIX003 — COMMIT publication phase boundary

Authority: **Candidate only**. This addresses R3 HIGH blocking observations in the same L07 ticket without reopening accepted upstream source archives.

## Linearization and error phases

| Phase | Operations | Failure response | COMMIT visibility |
|---|---|---|---|
| PREPARE | Exclusive destination, validate all persisted payloads, `before_publish`, write+fsync `.COMMIT.staged` | Remove unpublished staging; denial also removes provisional STRUCTURE | Absent |
| PUBLISH | **Only** `os.replace(.COMMIT.staged, COMMIT.json)` plus one in-memory `commit_published=True` under live `PublicationGate` | Revocation first: deny without publishing | Atomic |
| POST-PUBLISH | Directory fsync, root fsync and independent integrity/readback | Report `STORAGE_FAILED`; **no filesystem cleanup, repair or mutation of COMMIT/Manifest/payloads** | Remains published |

`commit_published` is monotonically set immediately after successful atomic rename, before any subsequent fsync or readback. Under an ambiguous exceptional rename outcome, existence of COMMIT is treated as possibly published and left untouched. Even if an injected exception occurs after rename, error classification never unlinks the public marker. The no-gate L06 path now uses the same staged+rename publication rather than creating visible COMMIT directly during a write/fsync.

## Authority and nonblocking

INVALIDATE/keeper observation loss accepted before rename denies the publication; accepted after rename cannot rewrite a committed hash. All payload writes, staged file fsync, directory/root fsync, readback, and shared-authority notification occur **outside** the publication gate's critical section. The real Chrome/SHIRABE and native Windows boundaries remain untested.

## Evidence and historical test compatibility

R3's 14 independent assessment tests are copied as **new candidate regression IDs** without modifying their assessment originals. Added 13 FIX003 fault injection tests cover late fsync/readback across invalidation and keeper loss, no-gate publication, ambiguous exceptional rename, and prepublication staging rollback. The 1,378 original baseline IDs and FIX002's 8 new IDs remain intact. Two assertions in legacy `test_l06_bundle_contract.py` are updated in place to reflect the explicit newly authorized invariant; the original test source bytes are preserved in the original external FIX002 ZIP, and a code diff is included. Historical `TEST_RESULTS` are not rewritten.

## Reporting

Store errors after publication remain `STORAGE_FAILED` to the capture-service public contract. A report of failure is **not** evidence that a visible COMMIT was undone, nor a permit to retry/rebind/recapture. Independent source verification, fixed-runtime full suite, and Human Gate remain mandatory.
