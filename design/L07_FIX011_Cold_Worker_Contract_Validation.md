# FIX011 Cold Worker Contract Validation

## Scope

The private worker optimization reduces initialization before START and AFTER terminal notification. It does not change capture authority, protocol messages, timeout, parent result verification or publication.

## Conditions and fallback

The schema, contract and matrix must match three exact raw SHA-256 values at contract initialization. The cached attestation is scoped to one worker process, like the existing canonical loader. A loaded jsonschema root or submodule disables the shortcut. Exact dict envelopes are independently revalidated by the existing spool validator and uniquely classified by the exact matrix. Non-dict or declined inputs reach the unchanged canonical API with the original object.

STRUCTURE uses a small compiler for the 17 keywords and four local definitions in the pinned format-free subtree. Unknown or unsupported schemas, rejected payloads, or shortcut errors use full canonical validation. Strict JSON, byte/depth constraints and the unchanged semantic checks remain in force. The private predicate only supplies successful validation; canonical fallback determines the original error.

## Order and parent verification

START and all subsequent permit calls keep their original order. STRUCTURE schema and semantic validation remain before AFTER identity. Invalid structure retains precedence over a simultaneous identity mismatch. The parent-facing _validate_structure always invokes the full canonical payload validator. The shared semantic body and the worker control-flow AST are checked against FIX010 by the implementation review and supplemental tests.

## Validation

The direct predicate is compared with the canonical validator for every reachable keyword, bounds, strict integer types, property closure, all 26 matrix rules, changed contracts and fallback inputs. Fresh isolated children forbid jsonschema import on positive paths and exercise real COMPLETE and epoch_after worker behavior. A review found non-dict envelope acceptance through the spool parser; the exact-dict guard and eight regression cases close that difference. Root final supplementary results are authoritative.

Finite differential tests and source review are development evidence; they are not a general theorem for arbitrary JSON Schema. Windows one-second deadline behavior requires native execution. No formal acceptance gate changes.
