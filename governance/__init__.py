"""Governance layers wrapped around the MCP server.

Two declarative documents and the enforcement around them. `policy.yaml`
decides who may call what; `sla.yaml` decides what has been promised about it.
Both are data rather than code, both fail closed, and both refuse to load a
document that is internally inconsistent.

  policy      — the declarative policy document and the decisions it yields
  verifier    — Entra ID token validation, implementing the SDK's TokenVerifier
  middleware  — enforcement, applied uniformly to every inbound request
  audit       — the authorization decision trail
  sla         — the service level document, and attainment computed from records
  sli         — measurement, and the deadline that bounds a latency objective
  approval    — the human-in-the-loop gate
  servicenow  — the ITSM connector, live or mocked
  devidp      — a local identity provider, so all of the above is testable
                without an Azure tenant
"""

from .audit import record
from .middleware import AUTHORIZATION_DENIED, PolicyEnforcementMiddleware
from .policy import CLASSIFICATIONS, Decision, Policy, PolicyError, Rule
from .sla import Sla, SlaError
from .sli import DEADLINE_EXCEEDED, ServiceLevelMiddleware
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
    "DEADLINE_EXCEEDED",
    "Decision",
    "EntraTokenVerifier",
    "JwksEndpoint",
    "Policy",
    "PolicyEnforcementMiddleware",
    "PolicyError",
    "Rule",
    "ServiceLevelMiddleware",
    "Sla",
    "SlaError",
    "StaticJwks",
    "describe_principal",
    "entra_issuer",
    "entra_jwks_uri",
    "record",
    "roles_from_token",
]
