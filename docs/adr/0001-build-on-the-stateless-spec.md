# ADR 0001: Build on the stateless 2026-07-28 spec

**Status:** accepted · **Date:** 2026-07-29

## Context

MCP before `2026-07-28` required an `initialize` handshake and an
`Mcp-Session-Id`. Server state was per-session, so a load balancer in front of
multiple replicas needed session affinity, and a replica restart dropped every
session bound to it.

The `2026-07-28` release removes both. Every request carries its own protocol
version and capabilities.

## Decision

Build on the stateless spec, and treat "any replica can answer any request" as
a property to preserve rather than a convenience to enjoy.

The server also answers legacy `2025-11-25` handshake clients on the same
endpoint, with no server-side flag. A platform rarely controls all of its
clients.

## Consequences

Good:

- Scales behind ordinary load balancing with no affinity configuration.
- Authorization, rate limiting and audit are simpler to reason about with no
  per-session state to keep consistent across instances.
- A replica restart affects only requests in flight.

The cost, which surfaced later: **anything spanning more than one round trip
has to carry its own state.** The approval flow needs to remember, between the
question and the answer, what was being approved. With no session to hang that
on, the state travels with the request — and therefore must be integrity
protected, because it passes through the client.

That is not a drawback of the decision so much as the shape it forces. It was
still nearly missed: the SDK defaults to sealing that state with a
process-local key, which reintroduces affinity silently. See
[ADR 0004](0004-no-hand-rolled-request-state-crypto.md) and
`governance/request_state.py`.

## Alternatives considered

**Stay on the handshake spec.** Better client compatibility at the time, at the
cost of the affinity requirement — the operational problem the whole exercise
is about. Rejected. Backwards compatibility was obtained instead by serving
both eras on one endpoint.
