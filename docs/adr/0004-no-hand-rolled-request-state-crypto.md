# ADR 0004: Do not hand-roll request-state cryptography

**Status:** accepted · **Date:** 2026-08-01 · **Supersedes:** an implementation
that shipped briefly on the `tranche-3` branch

## Context

The approval flow spans two round trips. The server returns an
`InputRequiredResult` with an opaque `request_state`; the client returns it on
the retry. **The state therefore passes through the client and is
attacker-controlled on the way back in.**

A gate that merely checks "was a state returned" is decorative. Two attacks
follow immediately:

1. Fabricate a state claiming approval was granted.
2. Subtler, and the one that matters: obtain approval for a cheap action and
   retry the *same* state with different arguments. Approve a low-urgency
   incident; raise a P1 that pages a rota at 03:00.

Defeating (2) requires binding the approval to the exact arguments it was
granted for, not merely to the tool.

## What was built first

An HMAC-SHA256 signed token over canonical JSON, binding tool, principal, a
SHA-256 digest of the arguments, an expiry and a nonce, with constant-time
comparison. Roughly 180 lines, with 14 tests including the argument-swap case.

The reasoning was right. The implementation was unnecessary.

## What was found

The SDK already does this. `RequestStateBoundary` (`mcp/server/request_state.py`)
sits outside the governance middleware and, for every state it mints and every
one it receives, enforces:

- **AES-256-GCM** — authenticated *encryption*, not just a signature, so the
  payload is not client-readable either. Over a key-rotation ring: the first key
  seals, every key unseals.
- **Request binding** — method, target tool, and a SHA-256 digest of the
  arguments. This is exactly the defence against attack (2).
- **Principal binding** — the (client, issuer, subject) triple as a salted hash.
- **Audience and expiry**, fail-closed in both directions, with the format
  version bound under the authentication tag (RFC 8725) so a token never names
  its own algorithm.

It was found the way these things usually are: the argument-swap test failed
with an error message that was not the one being asserted. The control already
existed, one layer out.

## Decision

Delete the hand-rolled layer. Rely on `RequestStateBoundary`.

`governance/approval.py` keeps only what the boundary has no opinion about:
what the state *means* — that minting one records "approval was requested" —
and reading the human's answer.

Tests were retargeted from the local re-implementation to the real control.
`test_arguments_swapped_after_approval_are_rejected_end_to_end` drives the
attack through a real client.

## Consequences

Good:

- No hand-written cryptography in the repository.
- Stronger properties than the deleted code provided: confidentiality, key
  rotation, and a versioned format.
- The tests now assert against the control that actually protects production.

Costs:

- A dependency on SDK internals that are not part of a stable API, against a
  pre-release version. Accepted: reimplementing them is strictly worse.
- One property was genuinely lost — the deleted code bound approval to the
  authenticated *principal name*, while the boundary binds the OAuth principal
  triple. In this server they resolve to the same caller, so nothing is weaker
  today. Worth rechecking if `bind_principal` is ever customised.

## Follow-on

The boundary defaults to `RequestStateSecurity.ephemeral()` — a process-local
key. Behind a load balancer that silently reintroduces session affinity: an
approval issued by one replica cannot be verified by another. This ADR's
investigation is what surfaced it. Fixed in `governance/request_state.py`, with
a shared key ring from `MCP_REQUEST_STATE_KEYS` and a startup warning when it is
absent. See [the rotation runbook](../runbooks/rotate-request-state-key.md).

## The general rule

Recognising that a security control is needed, and implementing that control,
are different skills with different failure modes. Reaching for the second when
the platform already provides the first is how repositories accumulate
cryptography nobody has reviewed. Check the framework first — especially when
the reasoning feels novel, because a spec author has usually had the same
thought.
