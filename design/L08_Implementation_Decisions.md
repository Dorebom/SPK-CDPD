# L08 Implementation Decisions

Role: EXECUTION / Authority: CANDIDATE_ONLY / Baseline: STATE005 and exact accepted L07 FIX012.

This document records implementation decisions for original L08 only. The source gate, final file pins and executed evidence are recorded in the Candidate `SOURCE_LOCK.json`, `REPORT.md`, `RESULT.json` and full Manifest. No management COMPLETE update, independent Assessment, Human Baseline adoption, Scheduler registration/start, Stable promotion or Release approval is performed here.

## D08-01: Preserve the approved core inside a separately versioned report

`report_engine.py` produces the unchanged `cdpd.chatgpt-report/v1` closed object. Its19 required fields and approved enums remain intact. Ticket-required `report.py` composes this under the `report` member of `cdpd.l08.report/v1`, adding traceability without editing the accepted core schema.

| L08 wrapper field | Meaning |
|---|---|
| `observation_mode` | Fixed `OFFLINE_REPLAY`; no new browser observation |
| `capture_id` | Verified original Bundle capture ID; null if unavailable/untrusted |
| `before_sha256` | Baseline `MANIFEST.json` raw-byte SHA-256 |
| `after_sha256` | Source `MANIFEST.json` raw-byte SHA-256 |
| `hash_scope` | Fixed `BUNDLE_MANIFEST_SHA256` |
| `source_commit_sha256` | Source `COMMIT.json` raw-byte SHA-256 |
| `before_normalized_sha256`, `after_normalized_sha256` | Canonical applicable feature/context projections; not Bundle/COMMIT hashes |
| `baseline_status` | `APPROVED` or `BASELINE_UNAPPROVED` |
| `failed_checks` | Fixed check ID, reason and UNKNOWN/BREAKING/REVIEW verdict derived from core reasons |

Approved core reason `BASELINE_NOT_APPROVED` maps to wrapper reason `BASELINE_UNAPPROVED`. The core's enum is not changed. Missing adoption always produces UNKNOWN; a new candidate or its generated metadata cannot supply approval.

A normalized hash excludes capture/time/Target/browser identity/sequence/bbox and includes recipe/catalog/normalization/policy/locale/viewport/browser profile plus applicable probes and safe local shapes. Its equality is not the overall report oracle. Fixed functional rules determine PASS/REVIEW/BREAKING, with UNKNOWN taking priority over incomplete evidence. Static compatibility does not prove Web send success, response content/causal identity, download correctness or SHIRABE WORK success.

## D08-02: Both inputs and every received declaration are verified

The accepted production `bundle_validation.verify_report_bundle` is applied independently to source and Baseline. It checks strict JSON, all closed schemas, full file set, Manifest/COMMIT/file hashes and sizes, profile identity/history/stage/structure semantics. No caller `prevalidated`, `required=false`, `all_required_ok`, or policy override can bypass it.

`validate_report_bytes` re-derives every core field. `validate_l08_report_bytes` additionally re-derives wrapper metadata. `verify_report_package` verifies the independent artifact, exact payload set, JSON semantics and deterministic Markdown bytes. A forged PASS, count, pointer, hash or omitted finding is rejected; the receiver never edits a false declaration into UNKNOWN. A separate valid UNKNOWN report may describe invalid evidence.

All parsing and rendering uses fixed vocabularies, typed counts and verified hashes/IDs. Error results omit arbitrary paths, URL queries, payloads, exception str/repr and conversation/input values. A valid opaque correlation field is not copied into the report. Approved privacy and N01/N02 behavior are preserved, including legitimate metadata-only NOT_RUN/null and nontruncated history-only PARTIAL; neither becomes success. ADR001 cumulative loss remains sticky.

## D08-03: Explicit external approval trust input, no adoption mutation

`baseline.py` accepts only a frozen `ApprovalAuthority` created by `verify_approval_record`. A separately supplied trusted SHA-256 must match the exact raw approval record, which also binds the Baseline Manifest, normalized profile and all context/catalog/policy fields. `approver_id`, timestamp and previous/new Manifest pins are recorded in the closed approval schema. Every use rechecks the record and target bytes.

The CLI takes `--approval-record` and independently supplied `--trusted-approval-sha256`. Merely hashing an unapproved record must never be used to invent trust. Test-only synthetic approval helpers do not constitute Human adoption. The CLI does not create an approval record, switch an approved pointer, or overwrite active/previous Baselines.

SHA-256 proves byte identity, not Human identity or safety against hostile same-user code. Local operator selection and the trusted approval process remain the explicit boundary. This Candidate implements read/verify, not a new authenticated adoption workflow.

## D08-04: Create separate immutable artifacts

`l08_artifact_store.py` stores a report, Baseline candidate or monitor status outside the capture Bundle. Original capture `RESULT.assessment_status` remains NOT_PERFORMED. Neither original payload, Manifest nor COMMIT is modified.

| Artifact kind | Directory ID | Payload |
|---|---|---|
| REPORT | `R-<artifact-manifest-sha256>` | `report.json`, `report.md` |
| BASELINE_CANDIDATE | `B-<artifact-manifest-sha256>` | `baseline-candidate.json`, exact source Bundle under `source_bundle/C-<capture-id>/` |
| MONITOR_STATUS | `M-<artifact-manifest-sha256>` | `monitor-status.json`, `monitor-status.md` |

Each has its own `MANIFEST.json` and `COMMIT.json`. It is create-only; the identical committed artifact may be reused after full byte verification. A partial/corrupt artifact is refused and never repaired in place. A nonblocking publish lock prevents concurrent writers; an unrecovered lock fails closed.

Before publication, payloads/Manifest are exclusively written, flushed and checked against provisional COMMIT bytes. COMMIT is staged then atomically renamed. After publication, any fsync/readback failure reports `POST_PUBLICATION_UNCONFIRMED` and preserves every observable committed byte. No failure path deletes or rewrites published evidence. Unpublished partial payloads are also preserved; only the private staged COMMIT may be removed. This is offline artifact reuse, not capture retry.

Capacity admission is bounded: report/monitor roots1GiB and250 artifacts, Baseline roots64MiB; one report/status artifact1MiB, one Baseline candidate10MiB+1MiB, including its artifact Manifest/COMMIT. The core JSON limit remains262144 bytes. Seven-day retention requires operator/consumer handling; this Candidate does not automatically delete protected/accepted/history artifacts. Native retention consumer integration remains NOT_RUN.

## D08-05: Windows storage admission remains a native boundary

Writing on Windows requires `--profile-directory` and the accepted `WindowsStoragePolicy` against the output root and a separate dedicated browser-profile directory. Both roots must already exist and be mutually disjoint. Admission checks local fixed NTFS, current-user ownership, protected private ACLs, path ancestors, reparse/cloud/offline attributes and registered synchronization roots. It does not create/change ACLs, owners or browser profiles. Unknown/native API failures reject admission.

The output root cannot be the source Bundle or lie inside an input artifact. No product flag or JSON field grants Windows storage authority. Synthetic injected policy tests verify control flow only. Native NTFS/ACL/Cloud Files/reparse/power-loss, full descendant/system-restart and real browser/SHIRABE acceptance remain NOT_RUN. The original Windows symlink limitation remains Windows-applicable and unverified.

## D08-06: Offline replay and scheduled capture are separate modes

`run_l08_monitor.py` reads an exact config file pinned by `--expected-config-sha256`, rejects unknown/missing fields and never dynamically loads a module/provider from JSON.

- `OFFLINE_REPORT` replays an explicitly selected committed source and optional adopted Baseline. The source COMMIT pin is mandatory in this mode. It calls the same offline report path and emits `scheduled_invocation=OFFLINE_REPLAY_ONLY`. This may reproduce PASS from prior complete evidence without checking whether Chrome is currently running; that PASS is explicitly about the pinned old observation.
- `SCHEDULED_CAPTURE` accepts exactly `schema`, `mode`, `output_directory`, `profile_directory`. The installed CLI has no trusted live-idle integration, so it emits BLOCKED/UNKNOWN with `TRUSTED_LIVE_IDLE_BOUNDARY_UNAVAILABLE`, zero capture attempts/sends/retries and no new Binding. It does not start Chrome, call a worker, or reuse a saved idle/browser boolean.
- A trusted in-process negative observer can establish `ChromeObservation.ABSENT`, yielding SKIPPED/UNKNOWN and `CHROME_NOT_RUNNING`. It cannot be injected from configuration. PRESENT or UNKNOWN still cannot authorize capture. This negative control is not a live Windows Chrome test.

Original L02 requires existing valid Binding, live Supervisor and trusted Consumer idle for a future authorized scheduled-capture integration; busy/missing Binding and the4h lease remain fail-closed boundaries. The accepted unavailable idle-proof path is not bypassed to advertise unattended24h monitoring.

## D08-07: Registration script defaults to a definition only

`register_monitor_task.ps1` validates absolute local files, safe task name/arguments, non-reparse paths and the config pin. Without `-Register`, it outputs a plan and invokes no Task Scheduler API or monitor process. `-WhatIf` prevents Scheduler APIs even when registration is requested.

Only an explicit later `-Register` operation reaches registration. Default registration is disabled; future automatic execution additionally requires `-EnableAutomaticStart`. The trigger must be at least one minute in the future. Existing tasks are rejected, not overwritten. The action directly invokes fixed sibling `run_l08_monitor.py` through the specified Python.exe with `-B`; no shell/evaluated command or arbitrary module is used. Current-user Interactive/Limited, IgnoreNew, no failure restart, no catch-up/wake and a five-minute execution limit are specified. There is no immediate-start operation.

The default30-day interval means every30days, not calendar-month scheduling. This execution generated/reviewed/tested the script only; it did not register, enable or start any task. Native PowerShell/Task Scheduler execution remains NOT_RUN.

## D08-08: Result and handoff boundaries

Report-command assessment exit codes are0 PASS,10 REVIEW,11 BREAKING,12 UNKNOWN. Input/read/storage rejection uses2 with BLOCKED/UNKNOWN. Baseline-candidate creation uses0 for successful creation while retaining UNKNOWN/BASELINE_UNAPPROVED; verification uses0 for package integrity+semantics and2 for rejection. A successful verify exit code does not assert report PASS.

Final test accounting is in root REPORT/RESULT. Source verification, focused synthetic product tests, historical accepted runs and native NOT_RUN are separate measurements. No L09 dependency changes or SHIRABE edits are made. Independent Assessment and any later Human decisions remain separate next steps.
