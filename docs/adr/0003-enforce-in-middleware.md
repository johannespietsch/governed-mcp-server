# ADR 0003: Enforce in middleware, not in tools

**Status:** accepted · **Date:** 2026-07-31

## Context

Authorization and human approval both need to run on every relevant call. The
SDK offers a context-tier middleware hook (`Server.middleware`) that sees every
inbound request before validation or dispatch.

## Decision

Enforce both in a single `ServerMiddleware`. Tools contain no authorization and
no approval code at all.

## Consequences

Good:

- **Uniform by construction.** There is no opt-in to forget. A tool author who
  knows nothing about the governance layer cannot ship an unguarded tool,
  because the policy denies anything it has not been told about
  ([ADR 0002](0002-policy-as-a-fail-closed-document.md)).
- **Denials happen before the handler runs**, so an unauthorized call never
  reaches a downstream system. This is the difference between refusing to open
  an incident and opening one, then refusing to say so.
- One place to read to answer "how is this enforced".
- `tools/list` filtering falls out naturally: the same component already knows
  the caller's roles.

Costs:

- Enforcement is not visible at the tool definition. A reader of
  `raise_shipment_incident` sees no gate; they have to know to look at
  `policy.yaml`. Mitigated by saying so in the tool's docstring.
- The middleware sees wire-shaped parameters, before model validation —
  `inputResponses`, not `input_responses`, and plain dicts rather than
  `ElicitResult` models. Both shapes are handled defensively, because pinning
  to one would break silently on an SDK bump.
- Registering it reaches for `server._lowlevel_server.middleware`. The lowlevel
  `Server.middleware` is documented as the extension point, but `MCPServer` does
  not re-expose it. Flagged inline as a bump risk against a pre-release SDK.

## Alternatives considered

**A decorator applied to each tool.** Visible where it applies, and opt-in.
Rejected: the failure mode of forgetting is an exposed tool.

**An ASGI middleware.** Wrong layer — it would have to parse JSON-RPC bodies to
find the tool name, and would not work for the in-memory transport the tests
rely on.

## Note on ordering

The middleware is appended, so it runs innermost: the SDK's OpenTelemetry
middleware and `RequestStateBoundary` both wrap it. This is deliberate. Rejected
calls are still traced, and request state is unsealed and re-bound before the
approval logic ever sees it.
