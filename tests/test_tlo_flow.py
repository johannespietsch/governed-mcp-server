"""The lighthouse use case, end to end.

Covers the Transport and Logistics Operations flow — assess a delayed shipment,
correlate it to a configuration item, and raise an incident behind a human
approval gate — plus the ServiceNow connector underneath it.

Everything here runs against the mock backend, so there is no instance and no
credential involved.
"""

from __future__ import annotations

from contextlib import contextmanager

import anyio
import pytest
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult

from governance import AUTHORIZATION_DENIED, Policy
from governance.devidp import DevIdentityProvider
from governance.servicenow import MockServiceNow, ServiceNowError
from governance.verifier import EntraTokenVerifier, StaticJwks

# --------------------------------------------------------------------------
# Connector
# --------------------------------------------------------------------------


def test_mock_finds_a_seeded_configuration_item():
    sn = MockServiceNow()
    assert anyio.run(sn.find_configuration_item, "CI-WMS-01") is not None
    assert anyio.run(sn.find_configuration_item, "CI-NOPE") is None


def test_mock_creates_and_then_finds_an_incident():
    sn = MockServiceNow()

    async def scenario():
        created = await sn.create_incident(
            short_description="test", configuration_item="CI-TMS-02", urgency="2"
        )
        assert created.number.startswith("INC")
        found = await sn.search_incidents(configuration_item="CI-TMS-02")
        return created, found

    created, found = anyio.run(scenario)
    assert created.number in [i.number for i in found]


def test_incident_against_an_unknown_ci_is_refused():
    """A CI that does not resolve must fail loudly, not open an unroutable ticket."""
    sn = MockServiceNow()
    with pytest.raises(ServiceNowError, match="no configuration item"):
        anyio.run(
            lambda: sn.create_incident(
                short_description="x", configuration_item="CI-DOES-NOT-EXIST"
            )
        )


def test_live_backend_refuses_to_start_without_credentials(monkeypatch):
    from governance.servicenow import LiveServiceNow

    for key in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ServiceNowError, match="credentials missing"):
        LiveServiceNow.from_environment()


# --------------------------------------------------------------------------
# The flow, through a governed server
# --------------------------------------------------------------------------


def build(roles: list[str]):
    from server import create_server

    idp = DevIdentityProvider()
    server = create_server(auth_mode="dev", dev_idp=idp)
    verifier = EntraTokenVerifier(
        issuer=idp.issuer, audience=idp.audience, key_source=StaticJwks(idp.jwks())
    )
    access_token = anyio.run(verifier.verify_token, idp.issue(roles=roles))
    assert access_token is not None
    return server, access_token


@contextmanager
def as_principal(access_token):
    reset = auth_context_var.set(AuthenticatedUser(access_token) if access_token else None)
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def approving(decision: bool):
    """An elicitation callback standing in for a human at the console."""

    async def callback(context, params):
        return ElicitResult(action="accept", content={"approve": decision})

    return callback


def declining():
    async def callback(context, params):
        return ElicitResult(action="decline")

    return callback


def test_assessment_recommends_enriching_an_existing_incident():
    """SHP-1002 maps to CI-WMS-01, which the mock seeds with an open incident."""
    server, token = build(["tlo.reader"])

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool("assess_shipment_delay", {"shipment_id": "SHP-1002"})
            body = result.structured_content
            assert body["delayed"] is True
            assert body["configuration_item"] == "CI-WMS-01"
            assert body["open_incidents"]
            assert "Enrich" in body["recommendation"]

    with as_principal(token):
        anyio.run(scenario)


def test_assessment_recommends_raising_when_nothing_is_open():
    """SHP-1004 maps to CI-TMS-02, which has no seeded incident."""
    server, token = build(["tlo.reader"])

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool("assess_shipment_delay", {"shipment_id": "SHP-1004"})
            body = result.structured_content
            assert body["open_incidents"] == []
            assert "Raise a new incident" in body["recommendation"]

    with as_principal(token):
        anyio.run(scenario)


def test_a_reader_may_assess_but_not_raise():
    """The read and write halves of the flow are separately authorized."""
    server, token = build(["tlo.reader"])

    async def scenario():
        async with Client(server, elicitation_callback=approving(True)) as client:
            await client.call_tool("assess_shipment_delay", {"shipment_id": "SHP-1004"})
            with pytest.raises(MCPError) as exc:
                await client.call_tool("raise_shipment_incident", {"shipment_id": "SHP-1004"})
            assert exc.value.code == AUTHORIZATION_DENIED

    with as_principal(token):
        anyio.run(scenario)


def test_operator_raising_an_incident_is_gated_then_allowed():
    server, token = build(["tlo.operator"])

    async def scenario():
        async with Client(server, elicitation_callback=approving(True)) as client:
            result = await client.call_tool(
                "raise_shipment_incident", {"shipment_id": "SHP-1004", "urgency": "2"}
            )
            assert result.structured_content["number"].startswith("INC")
            assert result.structured_content["configuration_item"] == "CI-TMS-02"

    with as_principal(token):
        anyio.run(scenario)


def test_a_declined_approval_stops_the_call():
    server, token = build(["tlo.operator"])

    async def scenario():
        async with Client(server, elicitation_callback=declining()) as client:
            with pytest.raises(MCPError) as exc:
                await client.call_tool("raise_shipment_incident", {"shipment_id": "SHP-1004"})
            assert exc.value.code == AUTHORIZATION_DENIED

    with as_principal(token):
        anyio.run(scenario)


def test_answering_no_stops_the_call():
    """Accepting the form while answering `approve: false` is still a refusal."""
    server, token = build(["tlo.operator"])

    async def scenario():
        async with Client(server, elicitation_callback=approving(False)) as client:
            with pytest.raises(MCPError) as exc:
                await client.call_tool("raise_shipment_incident", {"shipment_id": "SHP-1004"})
            assert exc.value.code == AUTHORIZATION_DENIED

    with as_principal(token):
        anyio.run(scenario)


def test_nothing_is_created_when_approval_is_refused():
    """The refusal has to stop the side effect, not just the response."""
    import server as server_module

    srv, token = build(["tlo.operator"])
    before = len(server_module.SERVICENOW.incidents)  # type: ignore[attr-defined]

    async def scenario():
        async with Client(srv, elicitation_callback=declining()) as client:
            with pytest.raises(MCPError):
                await client.call_tool("raise_shipment_incident", {"shipment_id": "SHP-1004"})

    with as_principal(token):
        anyio.run(scenario)

    assert len(server_module.SERVICENOW.incidents) == before  # type: ignore[attr-defined]


def test_arguments_swapped_after_approval_are_rejected_end_to_end():
    """The argument-swap attack, driven through a real client.

    Approve urgency 3, then replay the state that was handed back with
    urgency 1 — approve something routine, run something that pages people.

    The rejection comes from the SDK's `RequestStateBoundary`, which binds
    every sealed state to the method, the tool and a digest of the arguments.
    This asserts against that control rather than a re-implementation of it:
    an earlier version of this repository hand-rolled the same binding in
    `governance/approval.py`, which was redundant and has been removed.
    """
    server, token = build(["tlo.operator"])
    captured: dict[str, str] = {}

    async def scenario():
        async with Client(server) as client:
            # Drive the loop by hand so the retry can be tampered with.
            first = await client.session.call_tool(
                "raise_shipment_incident",
                {"shipment_id": "SHP-1004", "urgency": "3"},
                allow_input_required=True,
            )
            captured["state"] = first.request_state

            with pytest.raises(MCPError, match="requestState"):
                await client.session.call_tool(
                    "raise_shipment_incident",
                    {"shipment_id": "SHP-1004", "urgency": "1"},  # swapped
                    allow_input_required=True,
                    input_responses={
                        "approval": ElicitResult(action="accept", content={"approve": True})
                    },
                    request_state=captured["state"],
                )

    with as_principal(token):
        anyio.run(scenario)


def test_a_fabricated_approval_state_is_rejected():
    """A client cannot mint its own state: only sealed values are accepted."""
    server, token = build(["tlo.operator"])

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(MCPError, match="requestState"):
                await client.session.call_tool(
                    "raise_shipment_incident",
                    {"shipment_id": "SHP-1004"},
                    allow_input_required=True,
                    input_responses={
                        "approval": ElicitResult(action="accept", content={"approve": True})
                    },
                    request_state="awaiting-approval:raise_shipment_incident:forged",
                )

    with as_principal(token):
        anyio.run(scenario)


def test_answers_without_any_state_are_rejected():
    """Answering a question that was never asked is not approval."""
    server, token = build(["tlo.operator"])

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(MCPError, match="not approved"):
                await client.session.call_tool(
                    "raise_shipment_incident",
                    {"shipment_id": "SHP-1004"},
                    allow_input_required=True,
                    input_responses={
                        "approval": ElicitResult(action="accept", content={"approve": True})
                    },
                )

    with as_principal(token):
        anyio.run(scenario)


def test_every_tool_has_a_policy_entry():
    """Fail-closed only helps if someone notices. This is that someone.

    A tool registered without a policy entry is denied at runtime, which shows
    up as a puzzling refusal rather than an obvious mistake. Catching it here
    turns it into a failing test at the moment the tool is added.
    """
    from server import create_server

    server = create_server(auth_mode="off")
    policy = Policy.load("policy.yaml")

    async def names():
        return [t.name for t in await server.list_tools()]

    undeclared = sorted(set(anyio.run(names)) - set(policy.tools))
    assert not undeclared, f"tools with no policy entry: {', '.join(undeclared)}"
