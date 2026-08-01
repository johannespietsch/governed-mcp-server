"""Tests for tranche 1: token verification and per-tool authorization.

Runs with no network, no Azure tenant and no secrets — the development identity
provider mints real RSA-signed tokens in process, and the verifier under test
is the same one that runs against Entra.

The negative cases carry the weight. A verifier that accepts good tokens proves
very little; what matters is that it rejects a token aimed at another audience,
a token signed by the wrong key, and a token that claims to need no signature
at all.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing, contextmanager

import anyio
import httpx2
import pytest
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.exceptions import MCPError

from governance import AUTHORIZATION_DENIED, Policy, PolicyError
from governance.devidp import DevIdentityProvider
from governance.verifier import EntraTokenVerifier, StaticJwks, roles_from_token

POLICY = """
version: 1
default: deny
roles:
  tlo.reader: {description: read only}
  tlo.operator: {description: read and act}
tools:
  get_shipment_status:
    classification: internal
    allow_roles: [tlo.reader, tlo.operator]
  list_delayed_shipments:
    classification: internal
    allow_roles: [tlo.operator]
resources:
  "shipment://*":
    classification: internal
    allow_roles: [tlo.reader]
"""


@pytest.fixture
def policy() -> Policy:
    return Policy.from_mapping(yaml.safe_load(POLICY), source="<test>")


@pytest.fixture
def idp() -> DevIdentityProvider:
    return DevIdentityProvider()


@pytest.fixture
def verifier(idp: DevIdentityProvider) -> EntraTokenVerifier:
    return EntraTokenVerifier(
        issuer=idp.issuer,
        audience=idp.audience,
        key_source=StaticJwks(idp.jwks()),
    )


def verify(verifier: EntraTokenVerifier, token: str):
    return anyio.run(verifier.verify_token, token)


# --------------------------------------------------------------------------
# Token verification
# --------------------------------------------------------------------------


def test_valid_token_is_accepted(verifier, idp):
    token = verify(verifier, idp.issue(subject="ops@example.com", roles=["tlo.reader"]))
    assert token is not None
    assert token.subject == "ops@example.com"
    assert roles_from_token(token) == ["tlo.reader"]
    assert "mcp.invoke" in token.scopes


def test_token_for_another_audience_is_rejected(verifier, idp):
    """The confused-deputy defence: a token minted for a different service."""
    assert verify(verifier, idp.issue(audience="api://some-other-service")) is None


def test_token_from_another_issuer_is_rejected(verifier, idp):
    assert verify(verifier, idp.issue(issuer="https://login.microsoftonline.local/other/v2.0")) is None


def test_expired_token_is_rejected(verifier, idp):
    assert verify(verifier, idp.expired(roles=["tlo.reader"])) is None


def test_token_signed_by_the_wrong_key_is_rejected(verifier, idp):
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert verify(verifier, idp.issue(roles=["tlo.reader"], key=attacker_key)) is None


def test_unsigned_token_is_rejected(verifier, idp):
    """`alg: none` — accepted by naive verifiers, never by this one."""
    assert verify(verifier, idp.issue(roles=["platform.admin"], algorithm="none")) is None


def test_hmac_algorithm_is_not_configurable():
    """Algorithm confusion: signing with the public key as an HMAC secret.

    The constructor refuses the algorithm outright rather than relying on the
    caller to pass a safe list.
    """
    with pytest.raises(ValueError, match="non-asymmetric"):
        EntraTokenVerifier(
            issuer="https://example.test",
            audience="api://x",
            key_source=StaticJwks({"keys": []}),
            algorithms=("HS256",),
        )


def test_token_missing_required_claims_is_rejected(verifier, idp):
    assert verify(verifier, idp.issue(roles=["tlo.reader"], omit=("sub",))) is None


def test_garbage_is_rejected(verifier):
    assert verify(verifier, "not-a-jwt") is None


# --------------------------------------------------------------------------
# Policy decisions
# --------------------------------------------------------------------------


def test_role_grants_access(policy):
    assert policy.decide_tool("get_shipment_status", ["tlo.reader"]).allowed


def test_wrong_role_is_denied(policy):
    decision = policy.decide_tool("list_delayed_shipments", ["tlo.reader"])
    assert not decision.allowed
    assert decision.required_roles == ("tlo.operator",)


def test_no_roles_is_denied(policy):
    assert not policy.decide_tool("get_shipment_status", []).allowed


def test_unlisted_tool_is_denied_by_default(policy):
    """Fail closed: shipping a tool without a policy entry must not expose it."""
    decision = policy.decide_tool("delete_everything", ["tlo.operator"])
    assert not decision.allowed
    assert "default deny" in decision.reason


def test_resource_glob_matches(policy):
    assert policy.decide_resource("shipment://SHP-1001", ["tlo.reader"]).allowed
    assert not policy.decide_resource("secret://keys", ["tlo.reader"]).allowed


def test_visible_tools_is_scoped_to_roles(policy):
    assert policy.visible_tools(["tlo.reader"]) == {"get_shipment_status"}
    assert policy.visible_tools(["tlo.operator"]) == {"get_shipment_status", "list_delayed_shipments"}
    assert policy.visible_tools([]) == set()


def test_classification_is_carried_into_the_decision(policy):
    assert policy.decide_tool("get_shipment_status", ["tlo.reader"]).classification == "internal"


# --------------------------------------------------------------------------
# Policy document validation — failures that must happen at startup
# --------------------------------------------------------------------------


def test_undeclared_role_fails_to_load():
    """A typo in a role name is a startup error, not a rule that never matches."""
    bad = {
        "version": 1,
        "roles": {"tlo.reader": {}},
        "tools": {"t": {"allow_roles": ["tlo.raeder"]}},
    }
    with pytest.raises(PolicyError, match="undeclared role"):
        Policy.from_mapping(bad)


def test_allow_by_default_is_refused():
    with pytest.raises(PolicyError, match="default"):
        Policy.from_mapping({"version": 1, "default": "allow", "roles": {}, "tools": {}})


def test_unknown_classification_is_refused():
    bad = {
        "version": 1,
        "roles": {"r": {}},
        "tools": {"t": {"classification": "top-secret", "allow_roles": ["r"]}},
    }
    with pytest.raises(PolicyError, match="classification"):
        Policy.from_mapping(bad)


def test_unsupported_version_is_refused():
    with pytest.raises(PolicyError, match="version"):
        Policy.from_mapping({"version": 99})


def test_shipped_policy_file_is_valid():
    """The policy the server actually loads must parse and reference real roles."""
    loaded = Policy.load("policy.yaml")
    assert "get_shipment_status" in loaded.tools
    assert loaded.declared_roles


# --------------------------------------------------------------------------
# End-to-end enforcement through the server
# --------------------------------------------------------------------------


def build(roles: list[str], enforce: bool = True):
    """A dev-auth server, plus the verified `AccessToken` for a holder of `roles`.

    The in-memory transport has no HTTP layer, so nothing populates the auth
    context the way `AuthContextMiddleware` does on a real request. These tests
    set it directly — the token is still minted and verified for real, so what
    is being skipped is the transport, not the verification. The HTTP tests
    below cover the part this cannot reach.
    """
    from server import create_server

    idp = DevIdentityProvider()
    server = create_server(auth_mode="dev", dev_idp=idp, enforce=enforce)
    verifier = EntraTokenVerifier(
        issuer=idp.issuer, audience=idp.audience, key_source=StaticJwks(idp.jwks())
    )
    access_token = anyio.run(verifier.verify_token, idp.issue(roles=roles))
    assert access_token is not None
    return server, access_token


@contextmanager
def as_principal(access_token):
    """Stand in for what `AuthContextMiddleware` does on an HTTP request."""
    reset = auth_context_var.set(AuthenticatedUser(access_token) if access_token else None)
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def test_authorized_call_succeeds():
    server, access_token = build(["tlo.reader"])

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})
            assert result.structured_content["status"] == "delayed"

    with as_principal(access_token):
        anyio.run(scenario)


def test_call_without_the_required_role_is_refused_before_the_handler_runs():
    server, access_token = build(["some.unrelated.role"])

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(MCPError) as exc:
                await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})
            assert exc.value.code == AUTHORIZATION_DENIED

    with as_principal(access_token):
        anyio.run(scenario)


def test_anonymous_call_is_refused():
    server, _ = build(["tlo.reader"])

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(MCPError) as exc:
                await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})
            assert exc.value.code == AUTHORIZATION_DENIED

    anyio.run(scenario)


def test_tool_listing_is_filtered_to_the_callers_roles():
    """A caller should not be shown tools they cannot invoke."""
    server, access_token = build(["some.unrelated.role"])

    async def scenario():
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == []

    with as_principal(access_token):
        anyio.run(scenario)


def test_shadow_mode_audits_without_blocking():
    """Trialling a policy must not break traffic it would have denied."""
    server, access_token = build(["some.unrelated.role"], enforce=False)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})
            assert result.structured_content["id"] == "SHP-1002"

    with as_principal(access_token):
        anyio.run(scenario)


# --------------------------------------------------------------------------
# End-to-end over real HTTP — the bearer path the in-memory tests cannot reach
# --------------------------------------------------------------------------


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """A real uvicorn server with dev auth enabled, on a free port."""
    from server import create_server

    idp = DevIdentityProvider()
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    app = create_server(auth_mode="dev", dev_idp=idp, resource_url=url).streamable_http_app()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"

    yield url, idp

    server.should_exit = True
    thread.join(timeout=10)


def _client(url: str, token: str | None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


def test_http_request_without_a_token_is_challenged(live_server):
    url, _ = live_server
    response = httpx2.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    # RFC 9728: the challenge must point the client at the metadata document
    # describing how to obtain a token for this resource.
    assert "www-authenticate" in response.headers
    assert "resource_metadata" in response.headers["www-authenticate"]


def test_protected_resource_metadata_is_published(live_server):
    url, _ = live_server
    base = url.removesuffix("/mcp")
    metadata = httpx2.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"].rstrip("/") == url.rstrip("/")
    assert metadata["authorization_servers"]


def test_http_call_with_a_valid_token_and_role_succeeds(live_server):
    url, idp = live_server

    async def scenario():
        async with _client(url, idp.issue(roles=["tlo.reader"])) as client:
            result = await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})
            assert result.structured_content["status"] == "delayed"

    anyio.run(scenario)


def test_http_call_with_a_valid_token_but_no_role_is_denied(live_server):
    url, idp = live_server

    async def scenario():
        async with _client(url, idp.issue(roles=[])) as client:
            with pytest.raises(MCPError) as exc:
                await client.call_tool("get_shipment_status", {"shipment_id": "SHP-1002"})
            assert exc.value.code == AUTHORIZATION_DENIED

    anyio.run(scenario)


def test_http_token_for_another_audience_is_challenged(live_server):
    """The confused-deputy case, end to end rather than at the unit boundary."""
    url, idp = live_server
    token = idp.issue(roles=["tlo.reader"], audience="api://some-other-service")
    response = httpx2.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
