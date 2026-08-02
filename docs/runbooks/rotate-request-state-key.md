# Runbook: rotate the request-state signing key

**Applies to:** the key sealing `request_state` for the multi-round-trip
approval flow (`MCP_REQUEST_STATE_KEYS`).

**Run this when:** on the routine rotation schedule, when someone with access
to the secret leaves, or immediately on suspicion of disclosure.

**Impact if done wrong:** in-flight approvals are rejected. A user who has
just clicked "approve" sees the action fail and has to start again. No data
loss, no incorrect authorization — the failure is closed.

**Time:** three deployments, at least 10 minutes apart. Budget an hour.

---

## Background

The key is a rotation ring. **The first key seals; every key in the ring
unseals.** Rotation is therefore three phases, and each must be *fully rolled
out to every replica* before the next begins. Skipping a phase is what causes
rejected approvals: if one replica seals with a key another does not hold, any
approval crossing between them fails.

The state lives for 600 seconds (`RequestStateSecurity` default `ttl`). Phase
three must wait at least that long after phase two so nothing sealed under the
old key is still in flight.

## Preconditions

- [ ] You can write the secret and trigger a deployment.
- [ ] You know the current value (phase 1 needs it alongside the new one).
- [ ] No change freeze in effect.

## Procedure

### 1. Generate the new key

```bash
python server.py --print-state-key
```

32 random bytes, base64-encoded. Put it in the secret store now; do not paste
it into a ticket, a chat message, or a shell history you keep.

### 2. Phase one — every replica can unseal both

```
MCP_REQUEST_STATE_KEYS = <old>,<new>
```

Old still seals. New is accepted if it appears. Deploy to **all** replicas and
confirm the rollout completed before continuing.

Verify:

```bash
# Approvals still work end to end.
python -m pytest tests/test_request_state.py -q
```

### 3. Phase two — switch which key seals

```
MCP_REQUEST_STATE_KEYS = <new>,<old>
```

New seals. Old still unseals, which is what keeps approvals issued during
phase one working. Deploy to all replicas.

**Wait at least 10 minutes** (one full state TTL) before phase three.

### 4. Phase three — drop the old key

```
MCP_REQUEST_STATE_KEYS = <new>
```

Deploy. Rotation complete.

### 5. Confirm

```bash
# Should log: "request state sealed with a shared key ring (1 key(s))"
# Should NOT log any MCP_REQUEST_STATE_KEYS warning.
```

Then exercise one real approval through a client and confirm it completes.

## If it goes wrong

**Symptom: approvals fail with "Invalid or expired requestState".**

Check the server log for `requestState rejected ... unknown key`. That is a
replica being handed state sealed under a key it does not hold — almost always
a phase rolled out partially.

Rollback: set the ring back to `<old>,<new>` (phase one), which every replica
in any phase can unseal, and redeploy. Then restart the sequence, confirming
full rollout at each step.

**Symptom: the startup warning about `MCP_REQUEST_STATE_KEYS` appears.**

The variable is unset or empty, and the server has fallen back to a
process-local key. Approvals will fail across replicas and will not survive a
restart. Set the secret and redeploy.

**Symptom: `startup failed: MCP_REQUEST_STATE_KEYS entry 0 ...`**

The value is not valid base64, or is shorter than 32 bytes. Regenerate with
`--print-state-key`. The server refuses to start rather than run with a weak
key.

## In an actual compromise

Skip the phased rotation. Deploy `MCP_REQUEST_STATE_KEYS = <new>` directly.
Every in-flight approval is invalidated immediately, which is the intent — a
disclosed key means an attacker could mint approval state, and the phased
rollout exists only to protect user experience.

Then: review the audit stream for `approval` records with an `allow` outcome
over the exposure window, since those are the calls a forged state could have
produced.
