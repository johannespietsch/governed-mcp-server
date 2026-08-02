# Service level framework

What this layer promises about the services it fronts, how those promises are
measured, and what happens when one is broken. The commitments themselves live
in [`sla.yaml`](../sla.yaml); this explains the model behind them, so that
Phase 2 can onboard a domain by picking a tier rather than by negotiating a new
set of numbers each time.

## SLI, SLO, SLA

Used interchangeably almost everywhere, and the difference decides who gets
called when something breaks.

| | What it is | Where it lives | Who it binds |
| --- | --- | --- | --- |
| **SLI** | A measurement. "Ratio of successful `tools/call` responses." | `governance/sli.py`, emitted per call | nobody |
| **SLO** | An internal target on an SLI. "99.9% over 30 days." | `sla.yaml`, under `tiers:` | the platform team |
| **SLA** | The commitment made to whoever consumes the service, with a consequence attached. | `sla.yaml` plus whatever contract references it | the organisation |

The rule this framework follows: **the SLO is set tighter than the SLA**. If
the two are equal, the first breach of the objective is also a breach of the
commitment, and there is no interval in which to react. Everything below is
about SLOs. What is contracted externally is a business decision made on top of
them, and should be the looser number.

## The two indicators

Both are **request-based**, computed over events rather than over time.

**Availability** — good events over valid events. Not uptime: a stateless
replica set behind a load balancer is almost never wholly down, so an uptime
figure will read comfortably through an outage that made every second call
fail. What a caller experienced is the ratio.

**Latency** — the threshold at a percentile, over successful events only. "95%
of valid requests complete within 500 ms." Measured at this layer's boundary:
from the middleware receiving the call to the result leaving it. The caller's
own network time is not ours to promise, and including it would make the number
depend on where the client sits.

Averages are deliberately absent. A mean latency hides exactly the tail that
users notice, and a service can hold a flattering mean while a tenth of its
calls time out.

### What counts, and what does not

The denominator is the part that gets fudged, so it is written down.

| Event | Counts as | Why |
| --- | --- | --- |
| Tool returned a result | **good** | — |
| Tool returned `isError` | **bad** | spends error budget |
| Call cancelled at the tier deadline | **bad** | a call the caller could not use |
| Authorization denied (`-32003`) | **excluded** | the system did what it was configured to do |
| Paused for human approval | **excluded** | waiting on a person is not the service being slow |
| Tool not mapped to a service | **excluded** | nothing has been promised about it |

**Excluding authorization denials is the exclusion that matters.** If a denial
counts as a failure, every tightening of [`policy.yaml`](../policy.yaml) shows
up as a service level incident — and the predictable end of that is someone
loosening the policy to protect a dashboard. A denial is the control working.

Exclusions are counted and reported, never silently dropped. An exclusion that
shrinks the denominator without leaving a trace is indistinguishable from
traffic that stopped arriving.

**One known over-count.** A tool that raises because the caller asked for a
shipment that does not exist is a caller error, and mature practice keeps
4xx-equivalents out of the budget. This layer cannot yet tell that apart from a
downstream system falling over — both arrive as `isError` carrying a string — so
both count as failures. That over-reports, which is the safe direction: the
service looks worse than it is rather than better. The distinction becomes
available when the connector layer types its failures (roadmap tranche 2), and
belongs there rather than in pattern-matching on error messages here.

## Tiers

A tier is a bundle of objectives taken on wholesale. Onboarding a domain means
choosing one, not inventing numbers — which is what makes this reusable in
Phase 2 rather than a document about the TLO use case.

| Tier | Availability | Latency (p95) | Deadline | For |
| --- | --- | --- | --- | --- |
| **gold** | 99.9% | 500 ms | 5 s | production, no third-party system in the request path |
| **silver** | 98.5% | 2 s | 20 s | production, reaching a third-party system |
| **bronze** | 95% | 5 s (p90) | 30 s | newly onboarded or experimental domains |

Windows are 30 days and **rolling, not calendar**. A calendar window resets the
error budget on the first of the month, which is an incentive to spend what is
left of it on the 30th.

Bronze exists so that a new domain can be observed before anything is promised
about it. A domain that spends its first weeks in bronze produces real
attainment figures, and the tier it moves to afterwards is evidence-based
rather than aspirational.

## Error budgets

An availability objective of 99.9% is a statement that **0.1% of requests are
allowed to fail**. That allowance is the error budget, and it is a resource to
be spent rather than a line never to be approached — a service consistently at
100% is over-provisioned for what was promised, which is its own kind of waste.

The report prints the budget as a percentage consumed:

```
service               tier       events     avail   target   budget   latency   target  status
delay-assessment      silver       4210   99.121%  98.500%      59%     240ms   2000ms  ok
transport-visibility  gold        18400   99.842%  99.900%     158%      95ms    500ms  BREACH: availability
```

Above 100% is a breach. What happens then is the
[error budget runbook](runbooks/error-budget-exhausted.md); the short version
is that discretionary change to the affected service stops until the budget
recovers, which is what makes the budget a control rather than a metric.

### You cannot measure three nines on ten requests

A 99.9% objective permits one failure per thousand requests, so on a sample
below a thousand a single failure reads as a 100%-plus budget burn. The report
says `too few events (need 1000)` rather than declaring a breach it cannot
support. This matters more in a pilot than anywhere else: early traffic is thin,
and a framework that cries breach on eight requests teaches everyone to ignore
it before it ever has enough data to be right.

## Dependencies, and the arithmetic the loader enforces

Availability composes multiplicatively across everything in the request path. A
service reaching one 99% system cannot itself be better than 99%, however well
it is written.

```
transport-visibility   entra 99.99%                        ceiling 99.990%
delay-assessment       entra 99.99% x servicenow 99%       ceiling 98.990%
incident-raising       entra 99.99% x servicenow 99%       ceiling 98.990%
```

`governance/sla.py` computes that ceiling at load time and **refuses to start
if a service's tier promises more than its dependencies allow**. This is the
check that earns the file. It is why the ITSM services are silver and not gold:
not modesty, arithmetic. Anyone who tries to promote them gets a startup error
naming the dependency responsible.

Identity is declared once under `platform.depends_on` rather than repeated on
every service, because it is in the path of every governed call and a
per-service list is something to forget.

Two of the three numbers behind this are honest assumptions rather than
contracts, and `sla.yaml` records which is which in a `source:` field:

- **Entra ID, 99.99%** — Microsoft's published SLA.
- **ServiceNow, 99%** — an assumption. This is developed against a Personal
  Developer Instance, which carries no SLA at all. Replace it with the number
  from the actual subscription before anything downstream of it is signed.

An assumption written down as an assumption can be challenged. One quietly
rounded up cannot.

## Deadlines

Each tier carries a `deadline_ms`, and the middleware cancels a call that
exceeds it. **An objective with nothing enforcing it is a wish**: a call that
hangs does not breach a latency objective, it never appears in the numbers at
all, and the indicator reads clean straight through an outage.

The deadline sits well above the latency objective — it is there to kill hangs,
not slow-but-legitimate calls — and the loader refuses a document where the two
cross.

Deadline enforcement is **per service, and off for `incident-raising`**, for
two reasons:

1. That tool creates a record in ServiceNow. A call cancelled mid-flight may
   have already taken effect while the caller is told it failed, and a
   duplicate incident is a worse outcome than a slow one.
2. It is human-gated. Measurement wraps enforcement, so a deadline there would
   also be timing the round trip that pauses for approval.

The general rule: **enforce deadlines on reads, think hard about writes.**
`--no-deadlines` measures without cancelling anything, which is the posture to
run in first — this is the one control in the framework that can turn a slow
call into a failed one.

## Service map

`sla.yaml` is also the service map the JD asks for: each service records its
owner, its business service, the configuration items it runs on, and the tools
that constitute it.

| Service | Tier | Tools | Depends on | CIs |
| --- | --- | --- | --- | --- |
| `transport-visibility` | gold | `get_shipment_status`, `list_delayed_shipments` | — | CI-WMS-01, CI-TMS-02 |
| `delay-assessment` | silver | `search_incidents`, `get_configuration_item`, `assess_shipment_delay` | ServiceNow | CI-WMS-01, CI-TMS-02 |
| `incident-raising` | silver | `raise_shipment_incident` | ServiceNow | CI-WMS-01, CI-TMS-02 |

The `configuration_items` field is the join to the CMDB, and it is what makes
this a map rather than a list: a breach here becomes an incident against
something ServiceNow already knows about, correlated by the same identifiers
[`assess_shipment_delay`](../server.py) already uses.

Tools are grouped into a service when they fail together and are called for the
same reason. Finer grouping produces attainment computed over too few events to
mean anything; coarser grouping hides one broken tool inside a healthy average.
The read and write halves of the lighthouse use case are separate services for
the same reason they are separately authorized — they fail differently and are
operated differently.

Every tool with an authorization decision must also have a service level. That
is checked when the server starts, and again in the test suite against the
tools actually registered, because the two documents get edited by different
people for different reasons and drift between them is the expected failure.

## Support response targets

Each tier declares `response_targets` in minutes, keyed by ServiceNow urgency
(1 high, 2 medium, 3 low), so a breach maps onto the incident it will be raised
as.

**Nothing in this repository measures them.** They are a human commitment, and
the signal for them is in the ITSM tool, not here. They are declared rather than
omitted because an SLA framework that covers only what happens to be
instrumented is not a framework — it is a description of the monitoring. When
the ITSM integration is bidirectional, attainment against these becomes a
ServiceNow report, not an MCP one.

## Running it

Records go to their own stream, for the same reason audit records do: different
reader, different retention, and a stream meant to be parsed must not share a
handler with one meant to be read.

```bash
python server.py --auth dev --sla-log sla.jsonl
# ... traffic ...
python server.py --sla-report sla.jsonl
```

One record per `tools/call`:

```json
{"timestamp":"2026-08-02T17:49:27Z","service":"transport-visibility","tier":"gold",
 "tool":"get_shipment_status","latency_ms":1.168,"outcome":"good","reason":"completed",
 "protocol_version":"2026-07-28"}
```

Objectives are applied at **report** time, not baked into the records, so a
proposed tier can be evaluated against history — "would we have breached last
month under the tier we are about to promise" is the question worth being able
to answer before signing anything.

## Limits worth knowing

- **No principal in the SLI stream.** Attainment is a property of the service,
  not of who called it, and copying the principal into a second stream would
  double the surface carrying identity for no question this report can answer.
  The cost: per-consumer attainment is not supported. Adding it should mean a
  correlation id in both streams, not a name in this one.
- **The record sink is a log file.** Fine for a report over a window; not a
  time series, so there are no alerts, no burn-rate windows and no dashboard.
  That arrives with the observability tranche, which attaches an OpenTelemetry
  exporter — at which point these records become metrics and this file format
  becomes a fallback.
- **No availability measurement when nobody calls.** A request-based SLI cannot
  distinguish "healthy and idle" from "down and unreachable", because both
  produce no events. A synthetic probe is the standard answer and is not
  implemented here; `no data` in the report is the honest placeholder.
- **Nothing has run against a live tenant or a live ServiceNow instance**, so
  every latency figure in this repository comes from an in-process mock. The
  mechanism is exercised; the numbers are not evidence about production.
