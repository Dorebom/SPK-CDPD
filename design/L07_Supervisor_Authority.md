# L07 immediate restriction and bounded shared notification

This is an Execution design record for `supervisor_authority.py`. It does not
change the accepted L04 packages, close an Issue, or perform independent
Assessment.

## Why a separate adapter exists

The accepted `BindingGuard.set_diagnostic_only()` and `invalidate()` latch
local state and then synchronously invoke their shared authority. A blocked
authority callback must not block the Supervisor's heartbeat tick. S0 must
precede Worker launch, but operation denial must not wait for a stalled S0,
journal writer or finalizer.

`NonblockingRestriction.impose()` therefore performs only the accepted local
restriction transition in the calling thread. It sets `DIAGNOSTIC_ONLY` while
preserving an existing `RECONCILIATION_REQUIRED`. Explicit revocation sets
`BINDING_REVOKED` / `INVALIDATED` / terminal only when the guard is not already
terminal. An existing first reason and terminal state, including EXPIRED, are
left unchanged. These writes do not call the authority, clock, filesystem,
browser, permit callback, or journal.

The adapter requires the exact `BindingGuard` class, the original immutable
record object, generation and authority object. A locally replaced scope is
UNCONFIRMED and does not apply the old restriction to the replacement object.
The surrounding Supervisor must still serialize its own registration and
impose operations and preserve the accepted live-guard checks for every permit.

## Pinned private-state dependency

The private fields are used deliberately because the accepted public setters
combine the local latch and potentially blocking notification. The following
unchanged source pins are part of the Candidate dependency contract:

| Accepted file | SHA-256 |
|---|---|
| `packages/chatgpt-web-api/src/chatgpt_web_api/target_guard.py` | `5c78266433e5af5cdc12def1b78e1287aece51ea527cc5e25a7e64ee4d0a185e` |
| `packages/cdpd-chatgpt-adapter/src/cdpd_chatgpt_adapter/l03_binding.py` | `c548f3d03c57ce8ade5ed12d4657e9926acb838ac2d2a6d9450fc943e6dc2ff7` |

The required delivery/source verification pins these packages before import.
This adapter is not a promise of compatibility with future versions of those
private fields. A changed dependency requires review and new evidence.

## Shared acknowledgement

The external call runs in a daemon thread with the frozen binding ID and
generation. A separate timeout thread seals the result after at most 0.25 s
(default 0.2 s); property reads and completion also check the absolute deadline.
Blocking notification or readback never holds the local restriction lock.

Mode confirmation requires an exact `AuthoritySnapshot` for the frozen
binding ID, generation and canonical record hash, still BOUND/nonrevoked/
nonsuperseded with known continuity and a restrictive mode. Revocation requires
the same exact scope and an explicitly revoked/terminal shared state. A
different current generation is not retrospectively called revoked.

The accepted `L03BindingBridge.snapshot()` intentionally raises after terminal
revocation. For that exact class only, the adapter may acquire its existing
lock without waiting and read the existing record/generation/terminal state.
Exceptions from arbitrary duck-typed providers are not acknowledgements.

The public statuses are `NOT_IMPOSED`, `PENDING`, `CONFIRMED`, `UNCONFIRMED`.
Confirmation is a scoped notification fact, not admission to perform any Web
operation or capture. The Supervisor's permit wrapper must independently check
current stop/block state, exact context, keeper/lease and the accepted L06
permit contract. Terminal Worker notifications continue through the existing
authenticated first-cause bridge.

## Monotonic upgrades and uncertainty

Repeating the same or weaker restriction does not call the authority again.
A confirmed mode restriction may be upgraded with one scoped invalidation.
Upgrading while the mode notification is pending or unconfirmed immediately
revokes locally and seals UNCONFIRMED; it does not dispatch a concurrent or
replacement notification. Late mode completion cannot acknowledge revocation.

Timeouts, provider errors, nonapplying providers and mismatched snapshots stay
UNCONFIRMED. Late callback success does not change that status. Unknown shared
notification does not prove that previously created peer guards stopped using
an actually unchanged external authority. This is the inherited shared-authority
limit; the new local Supervisor/capture boundary remains denied.

## Component verification

`tests/test_l07_supervisor_authority.py` exercises immediate local denial,
blocked calls/readback, ignored updates, preserved reconciliation and first
cause, exact-generation late callbacks, idempotent/monotonic upgrades, immutable
uncertainty, replaced local scope and the accepted bridge's terminal readback.
The tests are Execution synthetic controls. They do not perform browser,
native Windows, current SHIRABE or independent Assessment acceptance.
