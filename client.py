"""
Client for the MCP server.

Two axes, same code path — transport and protocol era:

  python client.py                    # in-memory, no transport, 2026-07-28
  python client.py --http             # HTTP to a running `python server.py`
  python client.py --legacy           # in-memory, pre-2026 initialize handshake
  python client.py --http --legacy    # HTTP, handshake — the old-client case

`--legacy` is the point: the same server answers a client that knows nothing
about 2026-07-28, on the same endpoint, with no flag on the server side. That
matters for a platform that has to onboard clients it does not control.

The in-memory mode is worth keeping as the roadmap lands — it lets the
authorization and audit layers be tested without standing up a transport or an
identity provider, which keeps the suite fast enough to gate a pipeline.
"""

import argparse
import asyncio

from mcp import Client
from mcp_types import TextResourceContents


async def exercise(client: Client) -> None:
    tools = await client.list_tools()
    print("tools:", [(t.name, t.description, t.input_schema) for t in tools.tools])

    result = await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})
    print("get_shipment_status(SHP-1002) ->", result.structured_content)

    result = await client.call_tool("list_delayed_shipments", {"threshold_hours": 24})
    print("list_delayed_shipments(24) ->", result.structured_content)

    resource = await client.read_resource("shipment://SHP-1004")
    # contents is a union of text and blob resources; narrow before reading.
    detail = next((c.text for c in resource.contents if isinstance(c, TextResourceContents)), None)
    print("shipment detail ->", detail)

    print("protocol version negotiated:", client.protocol_version)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Exercise the MCP server.")
    parser.add_argument("--http", action="store_true", help="use Streamable HTTP instead of in-memory")
    parser.add_argument("--legacy", action="store_true", help="force the pre-2026 initialize handshake")
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    args = parser.parse_args()

    # "auto" probes `server/discover` and falls back to the handshake on its own;
    # "legacy" skips the probe and goes straight to `initialize`, which is what a
    # pre-2026 client does. Note you can't pin a handshake-era version here — the
    # server picks it (2025-11-25, the newest one both sides know).
    mode = "legacy" if args.legacy else "auto"

    if args.http:
        # Requires `python server.py` running in another terminal.
        async with Client(args.url, mode=mode) as client:
            await exercise(client)
    else:
        from server import mcp

        async with Client(mcp, mode=mode) as client:
            await exercise(client)


if __name__ == "__main__":
    asyncio.run(main())
