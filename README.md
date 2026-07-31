# Stateless MCP server — PoC

A minimal server on the new MCP **2026-07-28** spec (the release that dropped
yesterday, July 28). It's been tested end-to-end: in-memory and over real
HTTP, both included below.

## What "stateless" buys you

No `initialize` handshake, no `Mcp-Session-Id`. Every request carries its own
protocol version and capabilities. That means **any replica can answer any
request** — you can put this behind a plain round-robin load balancer with
zero sticky-session config, which was the main operational pain point with
MCP servers before this spec.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install "mcp[cli]==2.0.0rc1" uvicorn
```

`2.0.0rc1` is a pinned pre-release — the SDK's v2 line isn't stable yet, so
pin exact versions and expect to bump this. If you (or anything you publish)
depends on `mcp` in production, add an upper bound like `mcp>=1.27,<2` so the
eventual stable v2 doesn't surprise you.

## Files

- **`server.py`** — the MCP server. Two tools (`add`, `word_count`) and one
  resource template (`greeting://{name}`), built with `MCPServer` (the v2
  rename of `FastMCP` — same decorator API you'd already recognize).
- **`client.py`** — exercises the server along two independent axes,
  transport and protocol era:
  - `python client.py` — in-memory, no transport at all (fastest way to test)
  - `python client.py --http` — talks to a running `server.py` over
    Streamable HTTP, same as a real client would
  - add `--legacy` to either one to negotiate as a pre-2026 client (see
    "Old clients" below)

## Run it

Terminal 1:
```bash
python server.py
# Uvicorn running on http://127.0.0.1:8000/mcp
```

Terminal 2:
```bash
python client.py --http
```

Expected output:
```
tools: ['add', 'word_count']
add(2, 3) -> {'result': 5}
word_count(...) -> {'result': 4}
greeting -> Hello, Johannes!
protocol version negotiated: 2026-07-28
```

## Old clients

The claim that one endpoint serves both eras is worth verifying rather than
taking on faith, so `--legacy` makes the client negotiate the way a pre-2026
client does:

```bash
python client.py --http --legacy
# ...
# protocol version negotiated: 2025-11-25
```

Same server, same URL, no server-side flag — only the client's negotiation
policy changed. What's actually different:

- **Default (`mode="auto"`)** — the client probes `server/discover` at
  `2026-07-28`. Anything that isn't positive evidence of a modern server (a
  JSON-RPC error, an HTTP 4xx, an unparseable result, or a discover result
  advertising only handshake-era versions) falls back to `initialize`. The
  fallback is a denylist, so an unknown legacy server degrades rather than
  failing.
- **`--legacy` (`mode="legacy"`)** — skips the probe entirely and sends
  `initialize`, byte-identical to pre-2026 behavior. In-memory this also
  drives the real stream loop instead of the direct per-request path, so it
  exercises the old code path rather than just relabeling the version.

The negotiated version picks how every later request is stamped: `2026-07-28`
puts protocol version, client info and capabilities into each request's
`_meta`, which is what makes any replica able to answer it. `2025-11-25` sends
only the `Mcp-Protocol-Version` header, because the rest lives in the session
that `initialize` set up.

One limitation: you can't pin an arbitrary old version. `mode=` takes
`"auto"`, `"legacy"`, or a modern version string — passing `"2025-11-25"`
raises `ValueError` and tells you to use `mode="legacy"`. The handshake-era
version is the server's pick (newest both sides know, so `2025-11-25` here).
Simulating a genuinely older client (`2025-06-18`, `2024-11-05`) means
dropping to `ClientSession` and building the `InitializeRequest` yourself —
the server honors it, the `Client` wrapper won't emit it.

## Proving statelessness to yourself

Run `server.py` on two different ports (`--port 8000` and `--port 8001` — see
note below), put anything in front that round-robins between them (even a
one-line script alternating requests), and confirm every request succeeds
regardless of which instance answers. There's no shared session store to wire
up, which is the whole point of the release.

Note: `MCPServer("name", port=...)` is no longer valid in v2 — port config
moved to `uvicorn.run(..., port=...)` as shown in `server.py`, so change the
`port=8000` argument there directly to spin up a second instance.

## Where this goes next (for your agent system / filter.fyi)

- Swap the two toy tools for real ones and this is a legitimate remote MCP
  server — same shape scales to Cloudflare Workers, Lambda, or a plain
  Kubernetes deployment behind an ordinary load balancer.
- If a tool ever needs user input mid-call (a confirmation, a missing
  parameter), the old pattern was the server calling back to the client
  mid-request. That's gone in 2026-07-28 — the server now returns an
  "input needed" result with a token, and the client calls the tool again
  with the answer attached. Worth knowing before you port anything that used
  elicitation.
- OpenTelemetry tracing is on by default now (no-op until you attach an
  exporter), which is a nice freebie if you're already piping things through
  Logfire, Grafana, or similar for the agent system.
