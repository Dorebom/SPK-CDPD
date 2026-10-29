# L08 Baseline and report-core implementation decisions

Role EXECUTION; Authority CANDIDATE_ONLY. This implementation consumes the exact accepted FIX012 product input after the external source gate. It does not adopt a Baseline, update management state, register/start a task, perform independent Assessment, or grant Windows/native/Release acceptance.

## 1. Inputs and preserved contracts

The product modules use the accepted production `bundle_validation.verify_report_bundle` for source and Baseline. No test/design oracle is imported by product runtime; no `prevalidated`, caller-required flag, arbitrary policy, or dict `approved=True` entry exists.

The following inherited files are unchanged:

| Contract | Exact SHA-256 |
|---|---|
| `contracts/report_policy.json` | `618983645a8cba5f120c3a2f6df8d147344a956fa88736620f9eec484a05504b` |
| `contracts/report_v1.schema.json` | `b28b4b5bd5764131bfafaa3523da02f26233c41f0e5ac3eb638e767250c0e3e0` |
| `contracts/bundle_payload_v1.schema.json` | `2bb2483725d3042c3f53e7a44d616c4f949c0f3fe7868e716b6d74ca2eeca996` |
| `contracts/bundle_input_contract.json` | `12673cb12fa8761acb41e665bd8987674890b80f79eb9eb1c1f184926ffb361c` |

Rules follow the approved L02 FIX003 policy, closed report schema and design report §7. The preserved reference `references/approved_l02/tests/fix001_model.py` (`7eb5c9faf21d7025d0878a93fd6667deda6a5ae7a04abe14f2fe821e8f67a086`) is a **test-only differential reference**, not shipped runtime authority. New tests extract only its unchanged rule functions with AST, supply the same accepted production front-end, and compare all report fields. This reference comparison is Execution self-verification, not an independent Assessment.

## 2. Pure engine API

`baseline.py` owns normalized feature projection, immutable candidate metadata, and the typed approval-read boundary. `report_engine.py` owns the exact old report-v1 core. Ticket-required `report.py` separately owns the new L08 envelope, Markdown and filesystem composition.

```python
normalized_profile_sha256(baseline_blob) -> str
baseline_candidate_metadata(blob) -> dict
verify_approval_record(raw, *, trusted_approval_sha256, baseline_blob) -> ApprovalAuthority | None

derive_report(source_blob, baseline_blob=None, *, approval=None) -> dict
canonical_report_bytes(source_blob, baseline_blob=None, *, approval=None) -> bytes
validate_report_bytes(raw, source_blob, baseline_blob=None, *, approval=None) -> bool
```

Each normalizer and candidate operation runs the shared full bundle validation. Candidate metadata is produced only from an intrinsically eligible complete observation; incomplete/invalid Baselines are refused (`BASELINE_INELIGIBLE` or the accepted fixed bundle error). Metadata is always `CANDIDATE_UNAPPROVED` / `BASELINE_UNAPPROVED`, `automatic_approval=false`. No function changes a pointer or writes an approval record.

Source capture and assessment statuses remain separate. Report generation never changes a source Bundle's `RESULT.assessment_status=NOT_PERFORMED`, Manifest, COMMIT or any file. All engine methods are offline; code reads only fixed local contract files and never calls browser/network/process APIs. Raw inputs are immutable bytes, parsed into fresh objects; caller inputs are not repaired or mutated.

## 3. Human adoption read boundary

`contracts/baseline_approval_v1.schema.json` defines one closed record: `schema`, `decision=APPROVE`, `approver_id`, `approved_utc`, `previous_manifest_sha256`, `source_manifest_sha256`, `normalized_profile_sha256`, `recipe`, `catalog_sha256`, `normalization_sha256`, `policy_sha256`, `locale_class`, `viewport_class`, `browser_profile`.

The caller must provide the SHA-256 of a separately selected, trusted approval record. Missing/wrong/malformed trust input returns no authority. The exact raw approval bytes are hashed; no canonical rewrite is substituted. A report CLI or scheduler must never compute this hash from whichever record it finds and thereby turn it into authority. It must receive the independently selected pin, e.g. the explicitly supplied `--trusted-approval-sha256` runtime argument. Generating a record template does not approve it.

Verification checks the record's strict JSON/schema, then the full Baseline, functional eligibility, exact Manifest/normalized hash, and every context/catalog/policy binding. `previous_manifest_sha256` preserves the pinned old/new decision history; this read-only code does not move or assert the current accepted pointer. The declared operator identity/time are pinned metadata, not cryptographic authentication of a Human. The same-user local trust limit remains explicit.

`ApprovalAuthority` is a frozen typed carrier with raw bytes, trusted digest, source Manifest and normalized-profile pins. Its public constructor rejects callers lacking the private verification token. This is not a Python security sandbox: same-process trusted code can introspect state. More importantly, every derivation re-reads/revalidates the retained raw bytes/pin against the current Baseline, so an altered/rebound Baseline or changed carrier fields cannot pass through a cached approval bool.

Test helper `tests/l08_fixture.py::approval_for` creates explicit **synthetic test-only** trust records. Those are not Human adoption evidence and are never imported into product runtime or automatically selected by CLI.

The adoption context uses the exact inherited grammar: locale up to35 characters and its existing language-subtag pattern, viewport's closed enum, browser-profile's existing safe pattern/max128. Valid33–35-character locale controls protect compatibility. No stricter undocumented32-character limit is introduced.

## 4. Normalized Baseline hash and scope

The normalized object is canonical UTF-8 JSON with schema `cdpd.chatgpt-baseline-normalized/v1`, the seven context binding fields and the policy-catalog-ordered **applicable** features. Each feature records its four count/completeness values and safe tag/role/parent/open-shadow shape. It excludes capture ID, time, Target/browser identity, sequence, raw generic DOM tree and bbox coordinates.

Normalization does not determine overall status by hash equality. For example a required count may change while remaining within an unbounded allowed range; the exact normalized hash changes, but fixed policy determines whether that is significant drift. A safe feature-shape change is REVIEW. A generic bbox/layout change alone does not establish functional breakage.

No DOM labels, conversation/input values, URL queries, exception messages, selectors, arbitrary HTML or CDP response content are emitted. Schema-invalid data is rejected by the shared front-end, and the engine returns fixed reason codes without exposing rejected payloads. Allowed opaque event correlation metadata is also never rendered into the report core or normalized feature object.

## 5. Derivation and receiving contract

The fixed priority is UNKNOWN > BREAKING > REVIEW > PASS. All source and Baseline JSON/file-set/hash/semantic validation runs before evidence is consumed. Unknown prerequisite reasons follow the exact policy order; observed functional violation and significant drift reasons follow. An observed violating finding is retained when overall UNKNOWN takes priority.

Every applicable feature must have a complete probe; that rule includes optional attachment probes. Optional/hidden is not permission to omit evidence. A nonapplicable feature produces NOT_APPLICABLE. Required expected bounds are loaded from the fixed policy and cannot be overridden by input flags. Count/shape comparison uses the actual validated stored observations, never a caller-supplied drift bool.

The v1 report remains closed and contains its original19 fields. It cannot accept capture_id or new reason enums. The outer L08 envelope carries capture_id/before-after hashes/failed checks and maps v1 `BASELINE_NOT_APPROVED` to user-facing `BASELINE_UNAPPROVED`; the core is unchanged.

The receiver strictly parses up to262144 bytes, rejects duplicate keys/nonfinite numbers/invalid UTF-8/unknown fields and invalid integer-token types, validates the exact accepted schema, derives the entire report again, and compares canonical bytes. It rejects forged PASS, altered pointers/counts, omitted/duplicated findings and conflicting status. It does not repair a declared PASS into UNKNOWN. A separate legitimate UNKNOWN report can describe invalid inputs and pass receiving validation.

## 6. Existing issue and authority boundaries

N01: valid legacy metadata-only NOT_RUN/null or NONE remains accepted without success inference. N02: legitimate history-only PARTIAL may keep verified nontruncated structure while the report remains UNKNOWN. Both source and Baseline use the same accepted front-end. Exact original fixtures are replayed unchanged; malformed structure retention is rejected.

ADR001 cumulative loss/unknown remains sticky beyond a120-second window. Partial/history-invalid input cannot be made eligible by an approval record. No automatic reset/Preflight/new stream/recapture/rebind is introduced.

The engine creates no Web call, send, retry, process, COMMIT or Scheduler operation. Existing one-shot/max-send1/no-resend, Binding/nonce/PID/deadline and published-file immutability are preserved by leaving their accepted code unchanged. Actual task registration/start, native ACL/NTFS/reparse/power-loss behavior, live Chrome/CDP/ChatGPT/SHIRABE and L11/Release acceptance are not established by these synthetic engine tests.

## 7. Test evidence and ownership

New `tests/test_l08_report_engine.py` covers all four recipes, harmless layout, feature-shape drift, required absence, optional drift/incomplete probes, unknown precedence, complete approval binding and raw digest trust, invalid dict/bool approval, source/Baseline corruption, strict receiver forgery, exact N01/N02 fixtures, accepted context boundaries, secret-canary controls, deterministic offline output and immutable inputs. A13-case differential comparison checks exact parity against immutable approved rule functions with the accepted shared front-end.

Tests copy the existing L06 complete fixture into new Python objects. Accepted fixtures/tests/contracts are not edited. Fixed synthetic trust records do not constitute a new Baseline adoption. The parent Execution report owns final command, environment, result ledger and per-ID evidence accounting; earlier intermediate runs must not be added together.
