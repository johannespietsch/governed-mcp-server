"""Signing keys for the multi-round-trip request state.

The 2026-07-28 approval flow spans two round trips: the server returns an
`InputRequiredResult` carrying an opaque `request_state`, and the client sends
it back on the retry. The SDK's `RequestStateBoundary` seals that state with
AES-256-GCM so a client cannot forge or tamper with it.

**Which raises a question this server has to answer, and which its default
answer gets wrong.** `MCPServer` installs `RequestStateSecurity.ephemeral()`
when nothing is configured — a key generated in the process and held only by
that process. For a single worker that is fine. Behind a load balancer it is
not: replica A seals the state, the client retries, the balancer routes to
replica B, and B cannot unseal a token minted under a key it has never seen.
The approval is rejected and the call fails.

That is precisely the failure mode this repository claims not to have. The
whole argument for building on the stateless spec is that any replica can
answer any request, and an approval flow that silently requires session
affinity would take it back.

So the key is configuration, not an accident of process startup. Every replica
loads the same key from `MCP_REQUEST_STATE_KEYS`, and in deployment that is a
Key Vault secret. Without it the server still runs — refusing to start would
make the common single-process case needlessly painful — but it says loudly
that it is single-replica only.

`keys` is a rotation ring: the first key seals, every key unseals. Rotating
without dropping in-flight approvals means rolling out each phase fully before
starting the next:

    keys = [old, new]      # every replica can unseal both, still seals with old
    keys = [new, old]      # seals with new, still unseals old
    keys = [new]           # after one TTL, when nothing sealed under old remains

See `docs/runbooks/rotate-request-state-key.md`.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os

from mcp.server.request_state import RequestStateSecurity

logger = logging.getLogger("governance.request_state")

ENV_KEYS = "MCP_REQUEST_STATE_KEYS"

# AES-256. The SDK derives its actual encryption key from whatever it is given,
# but accepting a short secret here would let a deployment pass "changeme" and
# still look configured.
MINIMUM_KEY_BYTES = 32


class RequestStateKeyError(Exception):
    """The configured signing keys are unusable."""


def generate_key() -> str:
    """A fresh key, base64-encoded, for `MCP_REQUEST_STATE_KEYS`."""
    return base64.b64encode(os.urandom(MINIMUM_KEY_BYTES)).decode()


def load_keys(raw: str | None = None) -> list[bytes]:
    """Parse the comma-separated key ring. Empty list when unset."""
    raw = raw if raw is not None else os.environ.get(ENV_KEYS, "")
    if not raw.strip():
        return []

    keys: list[bytes] = []
    for index, chunk in enumerate(raw.split(",")):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            key = base64.b64decode(chunk, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RequestStateKeyError(
                f"{ENV_KEYS} entry {index} is not valid base64. Generate one with "
                "`python server.py --print-state-key`."
            ) from exc
        if len(key) < MINIMUM_KEY_BYTES:
            raise RequestStateKeyError(
                f"{ENV_KEYS} entry {index} is {len(key)} bytes; at least "
                f"{MINIMUM_KEY_BYTES} are required."
            )
        keys.append(key)

    if not keys:
        return []
    return keys


def build(*, audience: str | None = None, ttl: float = 600.0) -> RequestStateSecurity:
    """The request-state policy this server runs with.

    Falls back to a process-local key when nothing is configured, and says so —
    a warning here is the difference between "approvals fail intermittently
    under load" and "someone forgot to set the key".
    """
    keys = load_keys()
    if not keys:
        logger.warning(
            "%s is not set: sealing request state with a process-local key. "
            "Approvals will not survive a restart and will fail across replicas. "
            "Single-process use only.",
            ENV_KEYS,
        )
        return RequestStateSecurity.ephemeral(ttl=ttl, audience=audience)

    logger.info("request state sealed with a shared key ring (%d key(s))", len(keys))
    return RequestStateSecurity(keys=keys, ttl=ttl, audience=audience)
