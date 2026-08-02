# Runbook: revoke access

Three different things get called "revoke access". They have different blast
radii and different speeds, and reaching for the wrong one is the usual reason
an incident takes longer than it should.

| You want to stop… | Do this | Takes effect |
| --- | --- | --- |
| One person using one capability | Remove their Entra app role assignment | Next token issue (up to token lifetime) |
| Everyone using one tool | Edit `policy.yaml`, redeploy | Next deployment |
| A whole downstream system being reached | Rotate or revoke the connector credential | Immediately |

**Access tokens already issued remain valid until they expire.** Removing a
role assignment does not recall a token already in a client's hands. If the
requirement is "stop them *now*", removing the role is not sufficient — use one
of the faster controls below and remove the role as well.

---

## A. Remove a person's role

**When:** someone changes team, leaves, or an access review finds an over-grant.

1. In Entra, remove the user or service principal's app role assignment on the
   app registration representing this API.
2. Confirm the change.
3. Note the token lifetime. Until then, an already-issued token still carries
   the old `roles` claim and the server will honour it — role claims are read
   from the token, not looked up per request.

If that window is unacceptable, do A and B together.

**Verify:** after the window, the caller's `tools/list` should no longer
include the tools that role granted, and a call should audit as
`outcome: deny`, `reason: caller holds none of the roles this tool requires`.

## B. Withdraw a tool from everyone

**When:** a tool is misbehaving, or a capability must stop immediately
regardless of who holds which role.

Fastest safe option — narrow the grant to the break-glass role only:

```yaml
raise_shipment_incident:
  classification: confidential
  allow_roles:
    - platform.admin        # tlo.operator removed
```

Or remove the tool's entry entirely. The policy is fail-closed, so **a tool
with no entry is denied** — deleting the block is a complete withdrawal, and
`test_every_tool_has_a_policy_entry` will fail until the tool is also
unregistered in `server.py`, which is the reminder to finish the job.

Deploy. Verify with a caller who previously succeeded; expect error `-32003`.

**Do not** try to achieve this by deleting the tool function alone and leaving
the policy entry — that leaves a policy referring to something that no longer
exists, which is confusing to the next reader and to an audit.

## C. Cut off a downstream system

**When:** a connector credential is suspected disclosed, or the downstream
system must stop receiving traffic from this layer.

1. **Rotate or disable the credential at the source** — in ServiceNow, disable
   the integration user or reset its password. This is the only step that takes
   effect immediately and does not depend on a deployment.
2. Remove the values from the secret store (`SERVICENOW_INSTANCE`,
   `SERVICENOW_USER`, `SERVICENOW_PASSWORD`).
3. Redeploy with `--servicenow mock`, or with the credentials absent.

Note the behaviour if you do only step 2: the server **refuses to start** in
live mode without credentials (`startup failed: ServiceNow credentials
missing`). That is deliberate — it fails loudly rather than starting up with a
silently broken connector — but it means step 3 must accompany it, or the
service goes down rather than degrading.

**Verify:** connector calls fail closed, and the audit stream shows the
authorization decisions still being made and recorded.

## Afterwards

For anything triggered by suspected compromise rather than routine change:

- Review the audit stream over the exposure window for `allow` outcomes by the
  affected principal. Filter on `principal` and `outcome`.
- Denials are logged at `WARNING` and allows at `INFO`; if the environment
  drops `INFO`, the allow records that matter most to this review are the ones
  that will be missing. Check the retention configuration before concluding
  nothing happened.
- Record what was found in the incident, including "nothing" — an access review
  that concluded no misuse occurred is a finding worth keeping.
