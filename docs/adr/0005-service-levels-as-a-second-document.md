# ADR 0005: Service levels as a second declarative document

**Status:** accepted · **Date:** 2026-08-02

## Context

Phase 1 has to hand over a service level framework that Phase 2 reuses. The
options ran from a written document with no code behind it, through annotations
on the tools themselves, to a second declarative file alongside `policy.yaml`.

There was also a question of whether service levels belong in this repository at
all, given that the observability tranche is not built: without an exporter,
there is no time series, no dashboard and no alerting.

## Decision

A second declarative document, `sla.yaml`, loaded and validated at startup, with
measurement in middleware and attainment computed from the emitted records.

Three properties are load-time errors rather than review comments:

- A tier promising more availability than the service's dependencies allow.
- A deadline inside the latency objective it is meant to protect.
- A tool claimed by two services, or an availability written as `99.9` rather
  than `0.999`.

## Consequences

Good:

- **The dependency ceiling is enforced, not reviewed.** Availability composes
  multiplicatively, and this is the check that gets skipped — it produces a
  commitment that was impossible on the day it was signed and is discovered a
  quarter later. Now it is a startup error naming the dependency responsible.
  It is why the ITSM services are silver: arithmetic, not modesty.
- **Tiers make onboarding a choice rather than a negotiation.** A Phase 2
  domain picks gold, silver or bronze. That is the reusable artefact; a document
  describing the TLO use case's specific numbers would not have been.
- **The exclusions are in code and tested.** What counts as a failure is the
  part of an SLA that decides whether the number means anything, and writing it
  in prose alone leaves it to whoever computes the report that quarter.
- **`sla.yaml` doubles as the service map**, with owner, business service and
  CMDB configuration items per service — the other Phase 2 deliverable, and it
  belongs in the same file because a service map that disagrees with the SLA
  document is worse than either alone.

Costs:

- **Two documents to keep in step.** A tool needs an entry in both, and they
  are edited by different people for different reasons. Mitigated by a coverage
  check at startup and a test against the registered tools, but the drift is
  real and will recur.
- **A second measurement stream** with its own handler, on top of the audit
  stream. Justified on the same grounds — different reader, different
  retention — but it is a second thing to ship, rotate and retain.
- **Deadline enforcement can turn a slow call into a failed one.** The one
  control here with a failure mode of its own. Mitigated by making it per
  service, off by default in the document, and disableable with
  `--no-deadlines`.
- **The sink is a log file**, so there is no alerting and no burn-rate window.
  Real, and the reason the observability tranche is still on the roadmap.

## Alternatives considered

**Per-tool annotations** — `@tool(sla="gold")` next to the function. Visible
where it applies, and no second file to drift. Rejected for the same reason
authorization is not a decorator ([ADR 0003](0003-enforce-in-middleware.md)):
the promise would be spread across the codebase, unreviewable as a whole, and
unreadable by whoever signs it. A service level is a commitment made by an
organisation, and it should be diffable in a change request by someone who does
not read Python.

**A written framework with no code.** Faster, and it would have satisfied the
literal deliverable. Rejected because an unenforced SLA document drifts from
the system within one release, and because the dependency arithmetic — the part
most worth having — is exactly what a prose document does not check.

**Waiting for the observability tranche.** Defensible: measurement wants a time
series, and this has a log file. Rejected because the framework and the exporter
are separable, and the parts that need thinking about — what counts as a
failure, which dependency caps which promise, what happens when a budget is
spent — do not need an exporter to be got right. When one is attached, these
records become metrics and nothing about the model changes.

## Note on ordering

`ServiceLevelMiddleware` is registered *before* `PolicyEnforcementMiddleware`,
so it sits outside it and observes calls the policy refuses. Measuring inside
the gate would exclude denials by never seeing them, which produces an
identical availability figure while hiding how often callers are being turned
away. The exclusion should be a decision recorded in the stream, not an
accident of where the middleware was registered — so the denials are measured,
then explicitly excluded, and counted in the report.

The same ordering means a deadline would also bound the round trip that pauses
for human approval, which is the second reason `incident-raising` opts out of
deadline enforcement. The first is that cancelling a write mid-flight can leave
the incident created and the caller told it failed.
