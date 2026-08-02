"""Request-state signing keys, and the cross-replica property they protect.

The repository's central claim is that any replica can answer any request. The
approval flow spans two round trips, so it is the one path where that claim can
quietly stop being true: if each replica seals request state under its own key,
a retry that lands on a different instance is rejected and the approval fails.

`test_an_approval_from_one_replica_is_accepted_by_another` is the test that
matters. The one below it demonstrates the failure the shared key prevents, so
the fix does not silently stop being necessary.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager

import anyio
import pytest
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult

from governance import request_state
from governance.devidp import DevIdentityProvider
from governance.request_state import RequestStateKeyError
from governance.verifier import EntraTokenVerifier, StaticJwks

# --------------------------------------------------------------------------
# Key ring parsing
# --------------------------------------------------------------------------


def test_generated_keys_round_trip():
    key = request_state.generate_key()
    assert len(request_state.load_keys(key)[0]) >= request_state.MINIMUM_KEY_BYTES


def test_unset_is_an_empty_ring():
    assert request_state.load_keys("") == []
    assert request_state.load_keys("   ") == []


def test_a_rotation_ring_keeps_its_order():
    """The first key seals; every key unseals. Order is the rotation state."""
    first, second = request_state.generate_key(), request_state.generate_key()
    keys = request_state.load_keys(f"{first},{second}")
    assert keys == [base64.b64decode(first), base64.b64decode(second)]


def test_a_short_key_is_refused():
    """`changeme` base64-encodes fine and would otherwise look configured."""
    with pytest.raises(RequestStateKeyError, match="at least"):
        request_state.load_keys(base64.b64encode(b"changeme").decode())


def test_a_malformed_key_is_refused():
    with pytest.raises(RequestStateKeyError, match="base64"):
        request_state.load_keys("not base64 at all!!")


def test_missing_configuration_falls_back_to_a_process_local_key(caplog):
    """Permitted, because single-process use is common — but never silent."""
    with caplog.at_level("WARNING"):
        security = request_state.build()
    assert security is not None
    assert any(request_state.ENV_KEYS in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# The cross-replica property
# --------------------------------------------------------------------------


def replica(idp: DevIdentityProvider):
    """A server instance, as a second replica behind a load balancer would be."""
    from server import create_server

    return create_server(auth_mode="dev", dev_idp=idp)


@contextmanager
def as_principal(access_token):
    reset = auth_context_var.set(AuthenticatedUser(access_token) if access_token else None)
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def operator_token(idp: DevIdentityProvider):
    verifier = EntraTokenVerifier(
        issuer=idp.issuer, audience=idp.audience, key_source=StaticJwks(idp.jwks())
    )
    token = anyio.run(verifier.verify_token, idp.issue(roles=["tlo.operator"]))
    assert token is not None
    return token


def _approval_handoff(first, second, token, *, expect_rejection: bool = False):
    """Start an approval on `first`, finish it on `second`.

    A rejection is asserted inside the coroutine rather than around
    `anyio.run`: the client's task group repackages exceptions into an
    `ExceptionGroup`, which `pytest.raises(MCPError)` would not match.
    """

    async def scenario():
        async with Client(first) as client_a:
            pending = await client_a.session.call_tool(
                "raise_shipment_incident",
                {"shipment_id": "SHP-1004", "urgency": "3"},
                allow_input_required=True,
            )
            state = pending.request_state

        async with Client(second) as client_b:
            retry = client_b.session.call_tool(
                "raise_shipment_incident",
                {"shipment_id": "SHP-1004", "urgency": "3"},
                allow_input_required=True,
                input_responses={
                    "approval": ElicitResult(action="accept", content={"approve": True})
                },
                request_state=state,
            )
            if expect_rejection:
                with pytest.raises(MCPError, match="requestState"):
                    await retry
                return None
            return await retry

    with as_principal(token):
        return anyio.run(scenario)


def test_an_approval_from_one_replica_is_accepted_by_another(monkeypatch):
    """The property the whole repository is built on, for the approval path."""
    monkeypatch.setenv(request_state.ENV_KEYS, request_state.generate_key())

    idp = DevIdentityProvider()
    token = operator_token(idp)
    result = _approval_handoff(replica(idp), replica(idp), token)

    assert result.structured_content["number"].startswith("INC")


def test_without_a_shared_key_the_handoff_fails(monkeypatch):
    """Demonstrates the defect the shared key fixes.

    With a per-process key, replica B cannot unseal a state minted by replica A,
    so an approval that crossed instances would be refused — which is session
    affinity by the back door.
    """
    monkeypatch.delenv(request_state.ENV_KEYS, raising=False)

    idp = DevIdentityProvider()
    token = operator_token(idp)

    _approval_handoff(replica(idp), replica(idp), token, expect_rejection=True)


def test_a_rotation_ring_accepts_state_sealed_under_the_previous_key(monkeypatch):
    """Phase two of a rotation: seal with the new key, still unseal the old."""
    old, new = request_state.generate_key(), request_state.generate_key()

    idp = DevIdentityProvider()
    token = operator_token(idp)

    monkeypatch.setenv(request_state.ENV_KEYS, old)
    first = replica(idp)

    # The second replica has already moved to `[new, old]`.
    monkeypatch.setenv(request_state.ENV_KEYS, f"{new},{old}")
    second = replica(idp)

    result = _approval_handoff(first, second, token)
    assert result.structured_content["number"].startswith("INC")
