# Onboarding a second domain

How to add a domain — a new backing system and the tools over it — to this
layer, using the ServiceNow domain as the worked example.

Written for whoever industrialises the next use case. The first domain took a
day, mostly spent deciding the patterns below. The second should take an hour.

## What a domain consists of

| Piece | ServiceNow example | Where |
| --- | --- | --- |
| A connector | `governance/servicenow.py` | one module |
| A mock backend | `MockServiceNow` | same module |
| Tools over it | `search_incidents`, `raise_shipment_incident`, … | `server.py` |
| An access decision per tool | `policy.yaml` entries | policy |
| Tests, including failure paths | `tests/test_tlo_flow.py` | tests |

## Step 1: the connector

One module. A `Protocol` describing what the tools depend on, then two
implementations: live, and an in-process mock.

```python
class Warehouse(Protocol):
    async def find_stock(self, sku: str) -> StockLevel | None: ...
```

Rules that are not negotiable:

**Credentials come from the environment, and are read at construction, not at
import.** A missing secret must be a startup error with a readable message —
`from_environment()` raising is the pattern — not a stack trace on the first
tool call at 03:00.

**Never log a response body on an auth failure.** Systems echo request detail
in 401 bodies, including the credential they rejected. Report the status code.

**Project responses onto a narrow model.** Do not forward the source record.
A ServiceNow incident has 100+ columns of mostly free text; a warehouse record
will have its own equivalent. Whatever a tool returns can end up in a language
model's context, and the narrow model is the boundary where you decide what may.

**Ship the mock in the same commit.** Not for convenience — it is what makes
the failure paths testable. An expired credential, a lookup that does not
resolve, a duplicate write: these are the paths that never get exercised
against a live instance because provoking them there is awkward, and they are
where the bugs are.

## Step 2: the tools

Plain functions, registered in `create_server`. Two rules learned the hard way:

**Type the return.** A bare `dict` annotation produces no output schema and
`structured_content` comes back empty. Use a Pydantic model. It also gives the
policy layer a stable shape to attach classification to later.

**Split read from write.** The lighthouse flow is two tools —
`assess_shipment_delay` changes nothing, `raise_shipment_incident` does — with
different roles and different classifications. A read tool with a hidden write
is unusable in an agent loop, because the agent cannot look without also
touching production.

## Step 3: the access decision

Add an entry per tool to `policy.yaml`. **The policy is fail-closed, so a tool
with no entry is denied** — the server will start and the tool will simply
refuse every call, which is a confusing way to find out.

`test_every_tool_has_a_policy_entry` turns that into a failing test instead.
When it fails after you add a tool, it is doing its job.

```yaml
  find_stock:
    classification: internal
    description: Read-only stock lookup.
    allow_roles: [wms.reader, wms.operator, platform.admin]

  adjust_stock:
    classification: confidential
    allow_roles: [wms.operator, platform.admin]
    requires_approval: true
    approval_prompt: >-
      This will change recorded stock levels in the warehouse system. Approve?
```

Declare any new role under `roles:` first — a rule naming an undeclared role
stops the server from starting, which is deliberate.

### Choosing a classification

Follow the tool's **effect**, not its input. `raise_shipment_incident` takes an
identifier that is `internal` data and is `confidential` because of what it
does. See the [security baseline](security-baseline.md).

### Choosing whether to require approval

Ask: if an agent did this a hundred times in a loop, at 03:00, would anyone
mind? If yes, `requires_approval: true`. It costs a round trip and nothing else
— the tool needs no code for it.

## Step 4: new roles in Entra

Roles are app roles on the app registration representing this API, delivered in
the token's `roles` claim. Adding one:

1. Define the app role on the app registration.
2. Assign it to the users or service principals that need it.
3. Declare it under `roles:` in `policy.yaml` with a description saying who it
   is for — that description is what an access reviewer reads.

Naming convention: `<domain>.<capability>`, lowercase — `tlo.reader`,
`wms.operator`. `platform.admin` is break-glass and is intended to be assigned
temporarily and reviewed at every recertification.

## Step 5: tests

Cover, at minimum:

- the connector's failure paths against the mock;
- one authorized call succeeding;
- one call denied for a missing role;
- for gated tools, that a refusal **stops the side effect** — assert nothing
  was created, not merely that an error was returned.

## Checklist

- [ ] Connector module with a `Protocol`, a live backend and a mock
- [ ] Credentials from the environment, read at construction, clear error when absent
- [ ] Responses projected onto a narrow model
- [ ] Tools typed with Pydantic returns, read and write split
- [ ] Policy entry per tool, classification chosen by effect
- [ ] Approval on anything you would not want run unattended in a loop
- [ ] New roles defined in Entra and declared in `policy.yaml`
- [ ] Tests including failure paths and side-effect suppression
- [ ] `python -m pytest -q` green

## What is not yet reusable

Being honest about where the seams are rough:

- **There is no connector base class.** Retry, circuit breaking, rate limiting
  and secret resolution are per-connector today. That abstraction is tranche 2;
  with only one connector, extracting it now would be a guess.
- **Secrets are environment variables**, not Key Vault references. Tranche 2.
- **No per-connector observability.** Tranche 4.

Two connectors is the right moment to extract the base class — the shape will
be evident rather than speculative.
