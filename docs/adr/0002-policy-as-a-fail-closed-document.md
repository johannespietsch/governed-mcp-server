# ADR 0002: Authorization policy as a fail-closed document

**Status:** accepted · **Date:** 2026-07-31

## Context

Something has to decide which caller may invoke which tool. The obvious
implementations are a decorator on each tool, or a conditional inside it.

Two audiences need to answer questions about that decision, and neither reads
Python: a security reviewer asking "who can raise incidents", and an auditor
asking "who could have, in March".

## Decision

Express authorization as a YAML document (`policy.yaml`) mapping Entra app
roles to tools, with two properties enforced at load:

**Fail closed.** A tool with no entry is denied. Shipping a tool without an
access decision makes it unreachable — a visible bug — rather than public, a
silent one. `default: allow` is rejected by the loader; allow-by-default is not
a configuration option.

**Fail fast.** Every role a rule references must be declared under `roles:`, or
the server does not start. A rule naming a misspelled role can never match, and
a rule that never matches is invisible in testing and looks exactly like a
working deny.

## Consequences

Good:

- Reviewable by people who do not read the implementation; diffable in a change
  request; citable in an audit.
- Adding a tool forces an explicit access decision.
- Classification lives beside the grant, so audit and later redaction are
  driven by the same declaration.

Costs:

- The policy can drift from the code — a tool renamed in `server.py` and not in
  `policy.yaml` becomes silently unreachable. Mitigated by
  `test_every_tool_has_a_policy_entry`, which fails on any tool without an
  entry. That test is load-bearing, not decoration.
- Policy integrity now depends on the deployment pipeline rather than on
  anything the runtime can enforce. Recorded as an accepted risk in the
  [security baseline](../security-baseline.md).
- Role changes require a deployment. Acceptable: grants should be deliberate.

## Alternatives considered

**Per-tool decorators.** Co-located with the code they guard, and unreviewable
by a non-programmer. Also opt-in, so the failure mode is an unguarded tool.
Rejected on both counts.

**An external policy engine (OPA, Cedar).** The right answer at a larger scale,
with real cost: another service, another language, another thing to run. The
YAML document is a deliberate stepping stone — the decision function is already
isolated in `Policy.decide_tool`, so swapping the engine later means replacing
one module.
