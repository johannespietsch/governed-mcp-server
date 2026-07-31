"""
Minimal stateless MCP server (2026-07-28 spec) — proof of concept.

Run it:
    python server.py

It serves Streamable HTTP on http://127.0.0.1:8000/mcp and answers both
2025-11-25 (legacy, handshake-based) and 2026-07-28 (stateless) clients on
the same endpoint — no flag, no separate deployment.

Because there's no protocol-level session, you can point two, three, ten
instances of this same process at a shared port behind a plain round-robin
load balancer and any of them can answer any request. Try it: run this on
two ports and hit whichever one is free.
"""

from mcp.server import MCPServer

mcp = MCPServer("PoC Server")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def word_count(text: str) -> int:
    """Count words in a string."""
    return len(text.split())


@mcp.resource("greeting://{name}")
def greeting(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"


if __name__ == "__main__":
    import uvicorn

    # streamable_http_app() is the ASGI (Asynchronous Server Gateway Interface) app — this is what you'd put behind
    # nginx / a cloud load balancer / Cloudflare Workers in a real deployment.
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=8000)
