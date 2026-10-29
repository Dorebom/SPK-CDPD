# FIX004 Windows storage, publication, and synthetic-test contract

## Security boundary

- Production Supervisor.start validates the already approved WindowsStoragePolicy. Only local fixed NTFS, owner SID, protected DACL and restricted ACE, distinct dedicated profile, noncloud and nonreparse paths are admitted. Root cannot be created automatically before policy startup succeeds. Existing WindowsStoragePolicy is inherited unchanged.
- ProfileEvidenceStore requires a trusted callback on Windows; missing, False, truthy nonbool or exception is DENY (`WINDOWS_STORAGE_POLICY_UNVERIFIED`). Authorizer is re-evaluated after each relevant directory creation and before the final rename; no persistence-side permission can be inferred from 0700 mode.
- Filesystem is not a security boundary against hostile same-user code or privileged local administrators. Name and ACL rechecks are not atomic and cannot prove absence of adversarial same-user TOCTOU; these remain scope constraints.

## Publication and crash semantics

State machine:

```
STAGE_PAYLOAD_AND_MANIFEST -> FSYNC_STAGED_COMMIT -> CHECK_NATIVE_STORAGE_POLICY
-> [Gate grants] ATOMIC_RENAME_COMMIT -> PUBLISHED_UNREVERSIBLE
-> DIRECTORY_FLUSH_ATTEMPT -> READBACK -> RETURN
```

- Prepublication failures can remove ONLY staging. Postpublication failures return STORAGE_FAILED without mutation of already published COMMIT or payload hash set.
- A Win32 directory FlushFileBuffers failure is **not** treated as success. The operation requires FILE_FLAG_BACKUP_SEMANTICS; success is filesystem-dependent and does not imply hardware power-loss durability. If unsupported it is `WINDOWS_DIRECTORY_FLUSH_UNVERIFIED`, and the store does not falsely report fully durable publication. Separate Windows native crash/Job Object acceptance and product design decision remain required.
- No blocking filesystem/ACL/notification work inside PublicationGate's serialized rename action, no retry, repair or recapture.

## Synthetic Windows qualification limit

`conftest.py` test-only autouse fixture is a scoped in-memory grant under each test's own pytest tmp_path with lstat reparse checks. It never patches production `WindowsStoragePolicy`, never grants unrelated paths, never claims NTFS DACL authority or real directory flush. It can be disabled using `pytest.mark.no_windows_synthetic_capability` for negative cases. This fixture is not imported by the production package.

The synthetic directory flush models only namespace and observable file bytes for deterministic failure injection; native product durability is NOT_RUN. Evidence assessor must NOT accept these test PASS results as real ACL, OS transaction, power-loss or Job Object qualification.

## Windows harness controls

- Only `PYTEST_CURRENT_TEST` diagnostic ENV is shortened for oversized parametrized IDs. Full item.nodeid and TEST_RESULTS IDs are untouched.
- Windows symlink-privilege denial is explicit NOT_RUN with the privilege reason; no implicit elevated privileges.
- Existing 1,413 IDs preserved; FIX004 adds 8, total 1,421. Unselected single full suite required for native synthetic regression; special skip set must be explicitly audited.
