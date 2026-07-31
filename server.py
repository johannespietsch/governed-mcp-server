"""
Stateless MCP server — the transport baseline for a governed MCP layer.

Serves Streamable HTTP on /mcp and answers both 2025-11-25 (legacy,
handshake-based) and 2026-07-28 (stateless) clients on the same endpoint, with
no flag and no separate deployment.

Because 2026-07-28 has no protocol-level session — no `initialize` handshake,
no `Mcp-Session-Id` — every request carries its own protocol version and
capabilities, so any replica can answer any request. That removes the sticky
-session requirement that used to dictate how MCP servers were load balanced,
and it is why this is the starting point rather than an afterthought:
authorization, audit and rate limiting are all easier to reason about when any
instance can serve any caller.

The tools below are in-memory fixtures standing in for real integrations (see
the README roadmap). They exist to give the authorization and audit layers
something concretely shaped to wrap; they are not a data source.

    python server.py --port 8000
"""

from datetime import datetime, timedelta, timezone

from mcp.server import MCPServer
from pydantic import BaseModel, Field

mcp = MCPServer("Governed MCP Server")


class Shipment(BaseModel):
    """Internal record. Stands in for whatever the transport connector returns."""

    id: str
    origin: str
    destination: str
    status: str
    eta: datetime
    ci: str  # CMDB (Configuration Management Database) item for the handling system.


# Tool returns are modelled explicitly rather than as bare dicts: the SDK derives
# an output schema from the annotation, so a plain `dict` yields no structured
# content at all. Typed returns also give the policy layer a stable shape to
# attach data classifications to later.
class ShipmentStatus(BaseModel):
    id: str
    status: str
    origin: str
    destination: str
    eta: datetime
    configuration_item: str = Field(description="CMDB item for the handling system.")


class DelayedShipment(BaseModel):
    id: str
    destination: str
    eta: datetime
    configuration_item: str


def _fixtures() -> dict[str, Shipment]:
    """Static sample data. Replaced by the transport connector in tranche 2."""
    now = datetime.now(timezone.utc)
    rows = [
        Shipment(id="SHP-1001", origin="Rotterdam", destination="Duisburg",
                 status="in_transit", eta=now + timedelta(hours=6), ci="CI-WMS-01"),
        Shipment(id="SHP-1002", origin="Antwerp", destination="Venlo",
                 status="delayed", eta=now + timedelta(hours=31), ci="CI-WMS-01"),
        Shipment(id="SHP-1003", origin="Rotterdam", destination="Lille",
                 status="delivered", eta=now - timedelta(hours=12), ci="CI-TMS-02"),
        Shipment(id="SHP-1004", origin="Zeebrugge", destination="Koln",
                 status="delayed", eta=now + timedelta(hours=52), ci="CI-TMS-02"),
    ]
    return {s.id: s for s in rows}


SHIPMENTS = _fixtures()


@mcp.tool()
def get_shipment_status(shipment_id: str) -> ShipmentStatus:
    """Look up the current status and estimated arrival of a single shipment."""
    shipment = SHIPMENTS.get(shipment_id)
    if shipment is None:
        raise ValueError(f"unknown shipment: {shipment_id}")
    return ShipmentStatus(
        id=shipment.id,
        status=shipment.status,
        origin=shipment.origin,
        destination=shipment.destination,
        eta=shipment.eta,
        configuration_item=shipment.ci,
    )


@mcp.tool()
def list_delayed_shipments(threshold_hours: int = 24) -> list[DelayedShipment]:
    """List undelivered shipments arriving later than `threshold_hours` from now."""
    cutoff = datetime.now(timezone.utc) + timedelta(hours=threshold_hours)
    return [
        DelayedShipment(
            id=s.id,
            destination=s.destination,
            eta=s.eta,
            configuration_item=s.ci,
        )
        for s in SHIPMENTS.values()
        if s.status != "delivered" and s.eta > cutoff
    ]


@mcp.resource("shipment://{shipment_id}")
def shipment_detail(shipment_id: str) -> str:
    """Human-readable summary of a shipment, addressable as a resource."""
    shipment = SHIPMENTS.get(shipment_id)
    if shipment is None:
        return f"No shipment {shipment_id}."
    return (
        f"{shipment.id}: {shipment.origin} -> {shipment.destination}, "
        f"status {shipment.status}, ETA {shipment.eta:%Y-%m-%d %H:%M} UTC, CI {shipment.ci}."
    )


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the MCP server over Streamable HTTP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # streamable_http_app() is the ASGI (Asynchronous Server Gateway Interface)
    # app — this is what sits behind Azure API Management, nginx or any ordinary
    # load balancer in a real deployment. Run it on two ports and round-robin
    # between them to watch the statelessness claim hold.
    uvicorn.run(mcp.streamable_http_app(), host=args.host, port=args.port)
