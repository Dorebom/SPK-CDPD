# L07 FIX002 — COMMIT publication linearization

R2 F001/F002 found a gap after `COORDINATOR_COMMIT` permit returned true but before `COMMIT.json` became visible.

FIX002 introduces a per-live-binding `PublicationGate`. Local revocation latches the gate before shared notification. The Evidence Store writes and fsyncs `.COMMIT.staged` without the gate. Final publication executes only `os.replace(.COMMIT.staged, COMMIT.json)` while holding the gate lock.

Ordering:

1. revoke accepted first -> `invalidated=true` -> publish returns false -> staged COMMIT and unpublished STRUCTURE are removed;
2. atomic rename wins first -> COMMIT is visible -> revoke may then be accepted -> committed hashes remain unchanged.

Long payload I/O, staged-file fsync, directory fsync, and shared-authority callbacks are outside the publication critical section. This keeps the monitoring path independent from long storage/notification work while providing a single publication linearization point.
