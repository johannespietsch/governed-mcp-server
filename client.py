"""
Client for the stateless MCP PoC server.

Two axes, same code path — transport and protocol era:

  python client.py                    # in-memory, no transport, 2026-07-28
  python client.py --http             # HTTP to a running `python server.py`
  python client.py --legacy           # in-memory, pre-2026 initialize handshake
  python client.py --http --legacy    # HTTP, handshake — the old-client case

`--legacy` is the point of the PoC: the same server answers a client that
knows nothing about 2026-07-28, on the same endpoint, with no flag on the
server side.
"""

import asyncio
import sys

from mcp import Client


async def exercise(client: Client) -> None:
    tools = await client.list_tools()
    print("tools:", [(t.name, t.description, t.input_schema) for t in tools.tools])

    result = await client.call_tool("add", {"a": 2, "b": 3})
    print("add(2, 3) ->", result.structured_content)

    result = await client.call_tool("word_count", {"text": "the quick brown fox"})
    print("word_count(...) ->", result.structured_content)

    resource = await client.read_resource("greeting://Johannes")
    print("greeting ->", resource.contents[0].text)

    print("protocol version negotiated:", client.protocol_version)


async def main() -> None:
    # "auto" probes `server/discover` and falls back to the handshake on its own;
    # "legacy" skips the probe and goes straight to `initialize`, which is what a
    # pre-2026 client does. Note you can't pin a handshake-era version here — the
    # server picks it (2025-11-25, the newest one both sides know).
    mode = "legacy" if "--legacy" in sys.argv else "auto"

    if "--http" in sys.argv:
        # Requires `python server.py` running in another terminal.
        async with Client("http://127.0.0.1:8000/mcp", mode=mode) as client:
            await exercise(client)
    else:
        from server import mcp

        async with Client(mcp, mode=mode) as client:
            await exercise(client)


if __name__ == "__main__":
    asyncio.run(main())
