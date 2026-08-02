# Runbook: the error budget is spent

A service has consumed more than 100% of its error budget over the rolling
window. This is not an outage runbook — by the time this is read the failures
have usually stopped — it is what happens *afterwards*, and it is the step that
makes an error budget a control rather than a metric on a wall.

If the service is failing **right now**, this is the wrong document. Handle the
incident, then come back.

## 0. Confirm the budget is actually spent

```bash
python server.py --sla-report sla.jsonl
```

```
service               tier       events     avail   target   budget   latency   target  status
transport-visibility  gold        18400   99.842%  99.900%     158%      95ms    500ms  BREACH: availability
```

Three things to check before acting, in order, because each one has produced a
false alarm:

1. **Enough events?** A status of `too few events` means the objective cannot
   be evaluated on this sample, not that it was met — and not that it was
   breached. Stop here.
2. **Right window?** The objective is over 30 rolling days. A report run over a
   file containing an afternoon is answering a different question. Check the
   first and last `timestamp` in the record file.
3. **Real failures?** Filter the records for `"outcome":"bad"` and read the
   `reason`. `deadline exceeded` and `tool reported an error` are different
   problems with different owners, and a burn made entirely of the former is
   often a deadline set too tight rather than a service that got slower.

```bash
grep '"outcome":"bad"' sla.jsonl | python -c 'import json,sys,collections; print(collections.Counter(json.loads(l)["reason"] for l in sys.stdin))'
```

## 1. Freeze discretionary change to that service

Until the budget recovers, on the affected service only:

- No feature work, no new tools in that service, no non-essential dependency
  upgrades.
- Reliability work, and fixes for the cause of the burn, continue.
- Security fixes are never frozen. A frozen service that stays unpatched has
  traded one risk for a worse one.

The freeze is the entire point. An error budget nobody is willing to act on is
a chart. It is also the reason the budget belongs to the *service* and not to
the platform: freezing everything because one connector had a bad week is how
the policy gets abandoned.

Announce it to the service's consumers. A freeze nobody outside the team knows
about looks like the work has slowed down for no reason.

## 2. Establish what spent it

The three shapes this takes here, and the evidence for each:

| Shape | Looks like | Where to look |
| --- | --- | --- |
| A downstream system degraded | `bad` clustered in time, `reason: tool reported an error` | the dependency's own status; `docs/sla-framework.md` records what we assume of it |
| The deadline is too tight | `reason: deadline exceeded`, latency percentile near the deadline | the tier's `deadline_ms` against observed p95 |
| A real regression | burn spread evenly, latency shifted after a deploy | deployment history against the record timestamps |

Correlate with the audit stream by timestamp for what was being called and by
whom. The two streams share no identifier — a deliberate limitation recorded in
[the framework](../sla-framework.md#limits-worth-knowing) — so this is a manual
join over a narrow window, which is fine for an incident review and not fine as
a routine practice.

## 3. Decide what changes, and be honest about which

Exactly one of these is true, and the failure mode of this runbook is picking
the third without admitting it:

**The service should be more reliable.** Fix it. The freeze lifts when the
rolling window recovers, not when the fix merges — the budget is spent for the
next 30 days either way, and that is the intended cost.

**The objective was wrong.** Also legitimate: a tier chosen before any traffic
was observed is a guess, and bronze exists precisely so that guess can be
corrected. Lower the tier in `sla.yaml`, in a reviewed change, with the
attainment figures that justify it. This is not cheating if it is *written
down*; it is what the numbers are for.

**The dependency cannot support the promise.** If ServiceNow's real
availability is below the assumed 99%, the ceiling check in `sla.py` was
computed from a number that was never true. Update `dependencies.servicenow.availability`
and its `source:` field. The loader will then refuse to start if a service's
tier no longer fits, which is the point — it converts a silent impossibility
into a startup error.

**What is not on the list:** widening the exclusions so the failures stop
counting. Excluding a category of error to protect a number is the one change
that makes the whole framework worthless, and it is the tempting one because it
takes ten minutes and no argument. If an exclusion is genuinely justified,
argue it in [`docs/sla-framework.md`](../sla-framework.md) and change it
knowingly, in its own change request, never as part of an incident response.

## 4. Record it

In the incident, or in a review note:

- What was promised, what was delivered, over which window and how many events.
- What spent the budget.
- Which of the three decisions above was taken, and by whom.
- If the objective was lowered: the evidence, and when it will be revisited.

A service that has had its tier lowered twice without either change being
recorded looks, to the next person, like a service that was never reliable and
never will be. The record is what tells them which.

## Related

- [Service level framework](../sla-framework.md) — the model, the tiers, and
  what the exclusions are.
- [Revoke access](revoke-access.md) — if the burn turns out to be one caller
  hammering a tool they should not have.
