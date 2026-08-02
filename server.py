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
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from pydantic import BaseModel, Field

from governance import (
    audit,
    request_state,
    servicenow,
    EntraTokenVerifier,
    JwksEndpoint,
    Policy,
    PolicyEnforcementMiddleware,
    PolicyError,
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

# The ServiceNow backend in use. Rebound by `create_server`; the mock default
# means importing this module never reaches for a credential.
SERVICENOW: servicenow.ServiceNow = servicenow.MockServiceNow()


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


# ---------------------------------------------------------------------------
# ServiceNow domain — the ITSM side of the lighthouse use case
# ---------------------------------------------------------------------------


class IncidentSummary(BaseModel):
    number: str
    short_description: str
    state: str
    urgency: str
    configuration_item: str
    opened_at: str


class DelayAssessment(BaseModel):
    """The read-only half of the Transport and Logistics Operations flow."""

    shipment_id: str
    delayed: bool
    hours_late: float
    configuration_item: str
    configuration_item_found: bool
    open_incidents: list[IncidentSummary]
    recommendation: str


def _summarise(incident: servicenow.Incident) -> IncidentSummary:
    return IncidentSummary(
        number=incident.number,
        short_description=incident.short_description,
        state=incident.state,
        urgency=incident.urgency,
        configuration_item=incident.configuration_item,
        opened_at=incident.opened_at,
    )


async def search_incidents(configuration_item: str = "", limit: int = 10) -> list[IncidentSummary]:
    """Search open ServiceNow incidents, optionally for one configuration item."""
    found = await SERVICENOW.search_incidents(
        configuration_item=configuration_item or None, limit=limit
    )
    return [_summarise(incident) for incident in found]


async def get_configuration_item(name: str) -> dict:
    """Look up a configuration item in the CMDB by name."""
    item = await SERVICENOW.find_configuration_item(name)
    if item is None:
        return {"found": False, "name": name}
    return {
        "found": True,
        "name": item.name,
        "class": item.ci_class,
        "operational_status": item.operational_status,
    }


async def assess_shipment_delay(shipment_id: str) -> DelayAssessment:
    """Correlate a delayed shipment to its handling system and any open incidents.

    The lighthouse flow, read-only half: establish whether a shipment is late,
    which configuration item handles it, and whether ITSM already knows. Acting
    on the finding is a separate, separately-authorized tool — an assessment
    that quietly opened tickets would be unusable in an agent loop, because the
    agent would have no way to look without also touching production.
    """
    shipment = SHIPMENTS.get(shipment_id)
    if shipment is None:
        raise ValueError(f"unknown shipment: {shipment_id}")

    hours_late = max(0.0, (shipment.eta - datetime.now(timezone.utc)).total_seconds() / 3600 - 24)
    delayed = shipment.status == "delayed"
    item = await SERVICENOW.find_configuration_item(shipment.ci)
    open_incidents = await SERVICENOW.search_incidents(configuration_item=shipment.ci)

    if not delayed:
        recommendation = "No action: shipment is not flagged as delayed."
    elif item is None:
        recommendation = (
            f"Escalate manually: {shipment.ci} is not in the CMDB, so an incident "
            "raised against it would not route."
        )
    elif open_incidents:
        recommendation = (
            f"Enrich {open_incidents[0].number} rather than opening a duplicate — "
            f"{shipment.ci} already has an open incident."
        )
    else:
        recommendation = f"Raise a new incident against {shipment.ci}."

    return DelayAssessment(
        shipment_id=shipment.id,
        delayed=delayed,
        hours_late=round(hours_late, 1),
        configuration_item=shipment.ci,
        configuration_item_found=item is not None,
        open_incidents=[_summarise(i) for i in open_incidents],
        recommendation=recommendation,
    )


async def raise_shipment_incident(shipment_id: str, urgency: str = "3") -> IncidentSummary:
    """Open a ServiceNow incident for a delayed shipment.

    The write half of the flow. It carries no approval logic of its own — the
    policy marks it `requires_approval` and the middleware holds the call until
    a human agrees. Keeping the gate out of the tool is deliberate: a gate a
    tool author has to remember is a gate that will eventually be forgotten.
    """
    shipment = SHIPMENTS.get(shipment_id)
    if shipment is None:
        raise ValueError(f"unknown shipment: {shipment_id}")
    if urgency not in ("1", "2", "3"):
        raise ValueError("urgency must be '1' (high), '2' (medium) or '3' (low)")

    incident = await SERVICENOW.create_incident(
        short_description=f"Shipment {shipment.id} delayed to {shipment.destination}",
        configuration_item=shipment.ci,
        urgency=urgency,  # type: ignore[arg-type]
        description=(
            f"Shipment {shipment.id} from {shipment.origin} to {shipment.destination} "
            f"is flagged {shipment.status} with an estimated arrival of "
            f"{shipment.eta:%Y-%m-%d %H:%M} UTC. Raised from the MCP layer."
        ),
    )
    return _summarise(incident)


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
    servicenow_backend: Literal["mock", "live"] = "mock",
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
        # Shared across replicas, so an approval issued by one instance can be
        # verified by any other. Without this the SDK defaults to a per-process
        # key and the approval flow quietly requires session affinity — see
        # governance/request_state.py.
        request_state_security=request_state.build(),
    )

    global SERVICENOW
    SERVICENOW = servicenow.build(servicenow_backend)

    server.tool()(get_shipment_status)
    server.tool()(list_delayed_shipments)
    server.tool()(search_incidents)
    server.tool()(get_configuration_item)
    server.tool()(assess_shipment_delay)
    server.tool()(raise_shipment_incident)
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


def __getattr__(name: str) -> Any:
    """Build the module-level `mcp` on first access rather than at import.

    `client.py` does `from server import mcp` for the unauthenticated in-memory
    demo, but constructing a server as an import side effect means importing
    anything from this module builds a connector and emits its log line — which
    reads as nonsense when the operator asked for a different backend and the
    next line is a startup failure. Deferring it keeps `import server` free of
    side effects. The governed path is `--auth dev`.
    """
    if name == "mcp":
        instance = create_server(auth_mode="off")
        globals()["mcp"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    parser.add_argument(
        "--servicenow",
        choices=["mock", "live"],
        default="mock",
        help="ServiceNow backend (default: mock, requires no credentials)",
    )
    parser.add_argument("--print-token", metavar="ROLE", nargs="*",
                        help="with --auth dev, print a token carrying these roles and exit")
    parser.add_argument("--print-state-key", action="store_true",
                        help=f"print a fresh key for {request_state.ENV_KEYS} and exit")
    args = parser.parse_args()

    if args.print_state_key:
        print(request_state.generate_key())
        raise SystemExit(0)

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

    try:
        app = create_server(
            auth_mode=args.auth,
            dev_idp=idp,
            resource_url=f"http://{args.host}:{args.port}/mcp",
            enforce=not args.shadow,
            servicenow_backend=args.servicenow,
        )
    except (servicenow.ServiceNowError, PolicyError, request_state.RequestStateKeyError) as exc:
        # Misconfiguration, not a crash: a missing credential or a policy that
        # does not load should read as an operator error on one line, not as a
        # traceback that looks like a defect in the server.
        raise SystemExit(f"startup failed: {exc}") from None

    # streamable_http_app() is the ASGI (Asynchronous Server Gateway Interface)
    # app — this is what sits behind Azure API Management, nginx or any ordinary
    # load balancer in a real deployment. Run it on two ports and round-robin
    # between them to watch the statelessness claim hold.
    uvicorn.run(app.streamable_http_app(), host=args.host, port=args.port)
