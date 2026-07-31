"""Governance layers wrapped around the MCP server.

Tranche 1 (implemented): token verification and declarative per-tool
authorization, with an audit record for every decision.

  policy      — the declarative policy document and the decisions it yields
  verifier    — Entra ID token validation, implementing the SDK's TokenVerifier
  middleware  — enforcement, applied uniformly to every inbound request
  audit       — the authorization decision trail
  devidp      — a local identity provider, so all of the above is testable
                without an Azure tenant
"""

from .audit import record
from .middleware import AUTHORIZATION_DENIED, PolicyEnforcementMiddleware
from .policy import CLASSIFICATIONS, Decision, Policy, PolicyError, Rule
from .verifier import (
    ALLOWED_ALGORITHMS,
    EntraTokenVerifier,
    JwksEndpoint,
    StaticJwks,
    describe_principal,
    entra_issuer,
    entra_jwks_uri,
    roles_from_token,
)

__all__ = [
    "ALLOWED_ALGORITHMS",
    "AUTHORIZATION_DENIED",
    "CLASSIFICATIONS",
    "Decision",
    "EntraTokenVerifier",
    "JwksEndpoint",
    "Policy",
    "PolicyEnforcementMiddleware",
    "PolicyError",
    "Rule",
    "StaticJwks",
    "describe_principal",
    "entra_issuer",
    "entra_jwks_uri",
    "record",
    "roles_from_token",
]
