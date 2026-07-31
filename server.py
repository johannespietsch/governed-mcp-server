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

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from pydantic import BaseModel, Field

from governance import (
    audit,
    EntraTokenVerifier,
    JwksEndpoint,
    Policy,
    PolicyEnforcementMiddleware,
    StaticJwks,
    entra_issuer,
    entra_jwks_uri,
)
from governance.devidp import DEV_KEY_PATH, DevIdentityProvider

AuthMode = Literal["off", "dev", "entra"]

# The audience this server accepts tokens for. In Entra this is the Application
# ID URI of the app registration that represents *this* API. A token minted for
# anything else is rejected — see governance/verifier.py.
DEFAULT_AUDIENCE = "api://governed-mcp-server"
DEFAULT_RESOURCE_URL = "http://127.0.0.1:8000/mcp"

# Coarse gate at the transport layer: may this caller talk to the server at
# all. Which *tools* they may then invoke is decided by policy.yaml, so there
# is exactly one place to look when answering "why was this allowed".
REQUIRED_SCOPES = ["mcp.invoke"]


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


def shipment_detail(shipment_id: str) -> str:
    """Human-readable summary of a shipment, addressable as a resource."""
    shipment = SHIPMENTS.get(shipment_id)
    if shipment is None:
        return f"No shipment {shipment_id}."
    return (
        f"{shipment.id}: {shipment.origin} -> {shipment.destination}, "
        f"status {shipment.status}, ETA {shipment.eta:%Y-%m-%d %H:%M} UTC, CI {shipment.ci}."
    )


def create_server(
    *,
    auth_mode: AuthMode = "off",
    policy_path: str | None = "policy.yaml",
    dev_idp: DevIdentityProvider | None = None,
    resource_url: str = DEFAULT_RESOURCE_URL,
    audience: str = DEFAULT_AUDIENCE,
    enforce: bool = True,
) -> MCPServer:
    """Build the server with a given authorization posture.

    `auth_mode` is the only thing that differs between running this on a laptop
    and running it against a tenant:

      off    no token required, no policy — the stage-0 transport demo
      dev    tokens from the in-process development identity provider
      entra  tokens from a real Entra tenant, configured through the
             environment (MCP_ENTRA_TENANT_ID, MCP_RESOURCE_AUDIENCE)

    `dev` and `entra` differ only in where signing keys are fetched from. The
    verifier, the policy and the enforcement path are identical, so the tests
    that run against `dev` are exercising the code that runs in production.
    """
    verifier = None
    auth_settings = None

    if auth_mode == "dev":
        idp = dev_idp or DevIdentityProvider(audience=audience)
        verifier = EntraTokenVerifier(
            issuer=idp.issuer,
            audience=idp.audience,
            key_source=StaticJwks(idp.jwks()),
        )
        auth_settings = AuthSettings(
            issuer_url=idp.issuer,  # type: ignore[arg-type]
            resource_server_url=resource_url,  # type: ignore[arg-type]
            required_scopes=REQUIRED_SCOPES,
        )
    elif auth_mode == "entra":
        tenant_id = os.environ.get("MCP_ENTRA_TENANT_ID")
        if not tenant_id:
            raise SystemExit("auth mode 'entra' requires MCP_ENTRA_TENANT_ID to be set")
        audience = os.environ.get("MCP_RESOURCE_AUDIENCE", audience)
        verifier = EntraTokenVerifier(
            issuer=entra_issuer(tenant_id),
            audience=audience,
            key_source=JwksEndpoint(entra_jwks_uri(tenant_id)),
        )
        auth_settings = AuthSettings(
            issuer_url=entra_issuer(tenant_id),  # type: ignore[arg-type]
            resource_server_url=os.environ.get("MCP_RESOURCE_URL", resource_url),  # type: ignore[arg-type]
            required_scopes=REQUIRED_SCOPES,
        )

    server = MCPServer(
        "Governed MCP Server",
        token_verifier=verifier,
        auth=auth_settings,
    )

    server.tool()(get_shipment_status)
    server.tool()(list_delayed_shipments)
    server.resource("shipment://{shipment_id}")(shipment_detail)

    if auth_mode != "off" and policy_path is not None:
        policy = Policy.load(policy_path)
        # `Server.middleware` is the documented context-tier extension point:
        # it wraps every inbound request before validation or dispatch, which
        # is what lets authorization be applied uniformly instead of per tool.
        # Appending puts it innermost, so the SDK's OpenTelemetry middleware
        # still traces the requests this one rejects.
        server._lowlevel_server.middleware.append(  # noqa: SLF001 - no public accessor yet
            PolicyEnforcementMiddleware(policy, enforce=enforce)
        )

    return server


# Module-level instance for the unauthenticated transport demo, and for
# `client.py` running in-memory. The governed path is `--auth dev`.
mcp = create_server(auth_mode="off")


if __name__ == "__main__":
    import argparse
    import logging

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the MCP server over Streamable HTTP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--auth",
        choices=["off", "dev", "entra"],
        default="off",
        help="authorization mode (default: off, the transport-only demo)",
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="audit policy decisions without enforcing them, to trial a policy against real traffic",
    )
    parser.add_argument("--print-token", metavar="ROLE", nargs="*",
                        help="with --auth dev, print a token carrying these roles and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    audit.configure()

    # Persisted so that `--print-token` in a second terminal signs with the same
    # key the running server verifies against.
    idp = (
        DevIdentityProvider(audience=DEFAULT_AUDIENCE, key_path=DEV_KEY_PATH)
        if args.auth == "dev"
        else None
    )

    if args.print_token is not None:
        if idp is None:
            raise SystemExit("--print-token requires --auth dev")
        print(idp.issue(roles=list(args.print_token)))
        raise SystemExit(0)

    app = create_server(
        auth_mode=args.auth,
        dev_idp=idp,
        resource_url=f"http://{args.host}:{args.port}/mcp",
        enforce=not args.shadow,
    )

    # streamable_http_app() is the ASGI (Asynchronous Server Gateway Interface)
    # app — this is what sits behind Azure API Management, nginx or any ordinary
    # load balancer in a real deployment. Run it on two ports and round-robin
    # between them to watch the statelessness claim hold.
    uvicorn.run(app.streamable_http_app(), host=args.host, port=args.port)
