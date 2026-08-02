# Security baseline

What this MCP layer protects, how, and what it explicitly does not protect.
Written to be read by someone who did not build it — a security reviewer, or
the platform team inheriting it.

Scope: the MCP server and the governance layers around it. Out of scope: the
Azure landing zone it would be deployed into, and the security posture of the
systems it connects to.

## Data classification

Four tiers, lowest to highest. Every tool and resource in `policy.yaml`
declares one, and the declaration drives both the authorization decision and
what the audit trail records.

| Tier | Meaning | Handling |
| --- | --- | --- |
| `public` | Safe to disclose outside the organisation. | No restriction. |
| `internal` | Ordinary business data. Disclosure is unwelcome, not damaging. | Any authenticated caller holding a granting role. |
| `confidential` | Commercially sensitive, or capable of causing operational impact. | Narrower role grant. Consider requiring approval. |
| `restricted` | Personal data, credentials, or regulated content. | Not currently used. Would require field-level redaction before a tool returns it. |

Current assignments:

| Tool | Tier | Why |
| --- | --- | --- |
| `get_shipment_status`, `list_delayed_shipments` | `internal` | Operational data, read-only. |
| `search_incidents`, `get_configuration_item` | `internal` | ITSM read. Reveals infrastructure names. |
| `assess_shipment_delay` | `internal` | Correlation only; changes nothing. |
| `raise_shipment_incident` | `confidential` | Writes to production ITSM, pages an on-call rota, visible to a customer-facing service desk. |

The tiering rule that matters: **a tool's tier follows its effect, not its
input.** `raise_shipment_incident` takes a shipment identifier, which is
`internal` data, and is classified `confidential` because of what it does with
it.

## Trust boundaries

```
 Caller (agent, application, human-driven client)
   │  ▲ untrusted: everything below this line is attacker-controllable
   ▼  │
 Azure API Management ......... transport gate: token validation, rate limit
   │
   ▼
 MCP server replicas .......... authorization, approval, audit
   │
   ▼
 Connectors .................. ServiceNow, transport systems
```

Three things cross the boundary from the caller and must be treated as hostile:

1. **The access token.** Validated for signature, issuer, audience, expiry and
   required claims before anything else runs.
2. **Tool arguments.** Passed to connectors, which build queries from them.
3. **`request_state` on an approval retry.** Echoed by the client, therefore
   attacker-controlled on the way back in. Sealed and re-bound by the SDK — see
   [ADR 0004](adr/0004-no-hand-rolled-request-state-crypto.md).

A fourth crosses from the *other* direction and is easy to miss: **data
returned by a connector**. A ServiceNow incident description is free text
written by a human who is not necessarily trustworthy, and it flows into a
language model's context. See "Injection via tool results" below.

## Threat model (STRIDE)

Rated by residual risk *after* the mitigation described.

### Spoofing

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Caller presents a forged token | Signature verified against the issuer's published JWKS; keys cached by key id so rollover needs no restart. | Low |
| Token signed with the public key as an HMAC secret (algorithm confusion) | Asymmetric algorithms only, refused in the `EntraTokenVerifier` constructor rather than left to the caller to configure. `alg: none` rejected likewise. | Low |
| Token minted for a different service, replayed here (**confused deputy**) | Audience validated exactly against this server's own resource URI. | Low |
| This server's token forwarded to an upstream API (**token passthrough**) | Not done anywhere. Connectors authenticate with their own credentials. | Low |

### Tampering

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Approval state forged to claim consent | State is sealed with AES-256-GCM by the SDK's `RequestStateBoundary`; only tokens it minted unseal. | Low |
| **Approval obtained for one action, replayed for another** | State binds the method, the tool and a digest of the arguments. Approving `urgency: 3` and retrying `urgency: 1` is rejected. Covered by `test_arguments_swapped_after_approval_are_rejected_end_to_end`. | Low |
| Policy file altered to widen access | Out of scope for the runtime: `policy.yaml` is protected by repository review and deployment controls, not by the server. | **Medium — accepted.** The policy is the control; a compromised deployment pipeline defeats it. |

### Repudiation

| Threat | Mitigation | Residual |
| --- | --- | --- |
| "I never called that tool" | Every authorization and approval decision is written to a dedicated audit stream with principal, target, outcome, reason, classification and the roles held. | Low |
| Audit records lost or corrupted in the application log | Audit is a separate logger with its own handler and no propagation, emitting one JSON object per line. Retrofitting that split later means reprocessing history. | Low |
| Audit stream tampered with after the fact | **Not mitigated.** Records go to stdout. An append-only sink with retention controls is tranche 4. | **Medium — open.** |

### Information disclosure

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Caller discovers tools they cannot use | `tools/list` is filtered to the caller's roles. Listing a tool someone cannot call discloses the shape of systems they have no access to. | Low |
| Sensitive argument values captured in logs | Argument *names* are recorded; values are not. A log that quietly became confidential-tier is worse than one that never held the data. | Low |
| Over-broad connector responses reaching a model | Incident records are projected onto a narrow model. A ServiceNow incident carries 100+ columns of mostly free text. | Low |
| Rejection reason reveals which check failed | `verify_token` returns `None` for every failure; the distinction goes to the log, not the response. | Low |
| Connector credentials in an error message | ServiceNow errors report status codes, never response bodies — a 401 body can echo the credential that was rejected. | Low |

### Denial of service

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Unauthenticated request flood | Intended to sit behind API Management with rate limiting per subject. | **Open** until tranche 5. |
| Slow or hung identity provider stalls the server | JWKS fetch runs off the event loop, so one slow request does not block every concurrent request. | Low |
| Approval loop driven indefinitely | The SDK caps retry rounds (default 10) and request state expires after 600 seconds. | Low |

### Elevation of privilege

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Calling a tool without a granting role | Enforced in middleware before dispatch, so the handler never runs and no downstream system is touched. | Low |
| **A new tool shipped without an access decision** | Policy is fail-closed: a tool with no entry is denied. `test_every_tool_has_a_policy_entry` turns the omission into a failing test rather than a puzzling refusal. | Low |
| A rule referencing a misspelled role silently never matching | Policy load fails if a rule names an undeclared role. A never-matching rule is invisible in testing and looks exactly like a working deny. | Low |
| Approval bypassed by a tool author forgetting the gate | The gate is declarative and enforced in middleware; tools contain no approval code. | Low |

## MCP-specific threats

Not in STRIDE, and the ones most often missed in MCP deployments.

**Injection via tool results.** A connector returns text an untrusted human
wrote — an incident description, a work note — and it lands in a model's
context, where it may be read as instruction rather than data. *Not mitigated
here.* Narrow projection reduces surface but does not address it. The
mitigation is a client-side concern (treating tool output as data) plus
provenance marking on the server. Tracked for tranche 4. **Open.**

**Tool poisoning.** A tool's description is itself model-visible input; a
malicious or compromised description can steer an agent. Descriptions here are
authored in-repository and reviewed. It becomes a live risk the moment tool
definitions are loaded from a connector or a registry. **Low now, revisit in
tranche 2.**

**Rug-pull.** A tool that behaves benignly during review and changes later.
Mitigated by tools being code under review rather than remote definitions.
Reintroduced if connector manifests are ever fetched at runtime. **Low now.**

**Confused deputy.** Covered above under Spoofing. Called out separately
because it is the single most commonly skipped check in MCP servers: without
exact audience validation, any token a user holds for any service becomes a
key to this one.

**Over-broad agent autonomy.** An agent chaining read tools into a write tool
without a human. Addressed by splitting the lighthouse flow into a read half
and a write half with different roles, and gating the write. The residual is
stated plainly in the README: the client is trusted to relay the human's
decision, and nothing in the protocol lets the server verify a human was really
asked.

## Accepted risks

Recorded so they are decisions rather than oversights.

1. **The client is trusted to relay human consent.** Inherent to the protocol.
   A control needing a stronger guarantee belongs in ServiceNow's own approval
   workflow, where the human authenticates directly.
2. **The audit sink is not tamper-evident.** Tranche 4.
3. **No rate limiting in the server itself.** Deliberately API Management's
   job; do not duplicate it in two places.
4. **`policy.yaml` integrity depends on the deployment pipeline.** The policy
   is the control; nothing the runtime can do protects it from a compromised
   pipeline.

## Verification

Every mitigation marked Low above has a test. The negative cases are the ones
that carry the weight:

```bash
python -m pytest -q        # 55 tests
```

Not verified: anything against a live Azure tenant or a live ServiceNow
instance. Both paths are implemented; neither has been run.
