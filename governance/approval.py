"""Human approval for tools the policy marks `requires_approval`.

A gated tool does not run on first call. The server answers with an
`InputRequiredResult` carrying an elicitation, the client puts it to a human,
and the call is retried with the answer attached. That is the 2026-07-28
replacement for the elicitation callback a server used to make mid-request.

## Why this module is thin

The first version of this file hand-rolled an HMAC-signed state token binding
the tool, the principal, a digest of the arguments, and an expiry — because
`request_state` is echoed by the client and is therefore attacker-controlled
on the way back in. Without that binding the gate is decorative: approve a
low-urgency incident, retry the same state with `urgency: 1`.

That reasoning is right, and the SDK already implements it. `RequestStateBoundary`
(`mcp/server/request_state.py`) sits outside this middleware and, for every
`requestState` it mints and every one it receives, enforces:

* **Authenticity and confidentiality** — AES-256-GCM, with a key-rotation ring,
  a version bound under the authentication tag, and constant-time comparison.
  Handlers only ever see plaintext the server itself minted.
* **Request binding** — the method, the target tool, and a SHA-256 digest of
  the arguments. This is the argument-swap defence.
* **Principal binding** — the (client, issuer, subject) triple, stored as a
  salted hash, so an approval granted to one caller is not replayable by
  another.
* **Audience and expiry**, fail-closed in both directions.

Re-implementing that alongside it would mean shipping hand-written cryptography
next to a reviewed implementation, for no additional property. The redundant
layer was removed; `tests/test_tlo_flow.py` verifies the guarantees end to end
against the real control rather than against a local re-implementation.

## What is left for this layer

Only the part the boundary has no opinion about: *what the state means*. The
boundary guarantees a returned state is one this server minted for this exact
call. This module decides that minting one means "approval was requested", and
reads the human's answer out of the response.

## What is still trusted

The client. It relays the human's decision, and nothing in the protocol lets a
server verify a human was really asked — the elicitation response is as
trustworthy as the client presenting it. The gate therefore raises the bar from
"an agent can act unilaterally" to "the client must lie about consent", which
is a real improvement and not the same as proof. A control that needs the
stronger property belongs in the downstream system, where ServiceNow's own
approval workflow can enforce it.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

# Key for the approval question in the input-requests map. Server-assigned, and
# echoed back by the client alongside the human's answer.
APPROVAL_KEY = "approval"


class ApprovalError(Exception):
    """Approval was absent, refused, or could not be established."""


def new_pending_state(tool: str) -> str:
    """The plaintext state for a pending approval.

    Sealed by `RequestStateBoundary` on the way out and unsealed on the way
    back, so this only has to be meaningful to us, not unguessable — the nonce
    is there to make each round distinguishable in a log, not to carry weight.
    """
    return f"awaiting-approval:{tool}:{secrets.token_urlsafe(9)}"


def check(state: Any, answers: Any, *, tool: str) -> None:
    """Raise `ApprovalError` unless a human approved *this* call.

    `state` has already been unsealed and re-bound by the boundary; its mere
    presence proves this server minted it for this tool, these arguments and
    this principal, within the TTL. What remains is to confirm it is one of
    ours and that the answer is a yes.
    """
    if not isinstance(state, str) or not state.startswith("awaiting-approval:"):
        # Absent, or not a state this layer issued. A forged or replayed value
        # never reaches here — the boundary rejects it before the middleware
        # chain gets a chance to look.
        raise ApprovalError("no approval was requested for this call")

    if not isinstance(answers, Mapping) or APPROVAL_KEY not in answers:
        raise ApprovalError("the approval question was not answered")

    if not is_affirmative(answers[APPROVAL_KEY]):
        raise ApprovalError("a human declined the action")


def is_affirmative(answer: Any) -> bool:
    """Did the human actually say yes?

    Anything other than an explicit accept carrying `approve: true` is a no.
    `decline` and `cancel` both mean the action must not run, and a missing or
    malformed answer is not consent either.

    Answers arrive as wire mappings rather than `ElicitResult` models — the
    middleware sees params before validation — so both shapes are read.
    """
    action = _field(answer, "action")
    if action != "accept":
        return False
    content = _field(answer, "content") or {}
    if not isinstance(content, Mapping):
        return False
    return content.get("approve") is True


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)
