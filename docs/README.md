# Documentation

Operational and architectural documentation for the governed MCP layer. The
[project README](../README.md) covers what it is and how to run it; this
covers how it is meant to be operated, reviewed, and extended.

## For a security reviewer

- **[Security baseline](security-baseline.md)** — data classification tiers,
  trust boundaries, a STRIDE threat model with residual ratings, the
  MCP-specific threats that fall outside STRIDE, and four accepted risks
  recorded as decisions rather than oversights.

## For whoever operates it

- **[Rotate the request-state signing key](runbooks/rotate-request-state-key.md)**
  — the three-phase rotation, what breaks if a phase is rolled out partially,
  and what to do differently in an actual compromise.
- **[Revoke access](runbooks/revoke-access.md)** — the three different things
  that get called this, their blast radii and speeds, and why removing an Entra
  role does not stop someone immediately.

## For whoever extends it

- **[Onboarding a second domain](onboarding-a-domain.md)** — the worked pattern
  from the ServiceNow domain, a checklist, and an honest list of the seams that
  are not yet reusable.

## Why things are the way they are

Architecture decision records, newest last:

- **[0001 — Build on the stateless spec](adr/0001-build-on-the-stateless-spec.md)**
  · what it bought, and the multi-round-trip cost it forced.
- **[0002 — Policy as a fail-closed document](adr/0002-policy-as-a-fail-closed-document.md)**
  · why authorization is YAML rather than decorators, and why
  allow-by-default is not a configuration option.
- **[0003 — Enforce in middleware, not in tools](adr/0003-enforce-in-middleware.md)**
  · uniform by construction, and the visibility cost that comes with it.
- **[0004 — Do not hand-roll request-state cryptography](adr/0004-no-hand-rolled-request-state-crypto.md)**
  · a signed-state implementation that was written, then deleted once the SDK
  was found to already provide it — and the load-balancer defect that
  investigation surfaced.

## Status

Tranches 1, 3 and 6 of the [roadmap](../README.md#roadmap) are implemented.
Tranches 2, 4 and 5 — the connector abstraction, observability export, and
Azure deployment — are designed and marked as such throughout.

Nothing has been run against a live Azure tenant or a live ServiceNow instance.
Both paths are implemented; neither is verified. Where these documents describe
something unverified, they say so.
