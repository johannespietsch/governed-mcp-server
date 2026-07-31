"""Bearer token verification against Microsoft Entra ID.

Implements the SDK's `TokenVerifier` protocol: validate a JSON Web Token (JWT)
signed by the identity provider and turn it into an `AccessToken` the rest of
the server can reason about.

The three checks that matter, in the order they bite:

1. **Signature, against the issuer's published keys.** Keys are fetched from
   the JSON Web Key Set (JWKS) endpoint and cached by key id, so key rollover
   is picked up without a restart.
2. **Algorithm allowlist.** Only asymmetric algorithms are accepted. Without
   this, a token signed `HS256` using the *public* key as the shared secret
   validates — the classic algorithm-confusion attack — and `alg: none` is
   accepted by naive verifiers outright.
3. **Audience, exactly.** The token must name *this* server as its audience.
   This is the defence against the confused deputy: a token a user legitimately
   issued to some other service must not be replayable here, and a token this
   server receives must not be forwardable upstream. It is the single most
   commonly skipped check in MCP deployments and the reason token passthrough
   is called out as an anti-pattern in the specification.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anyio.to_thread
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger("governance.auth")

# Asymmetric only. See the module docstring — permitting an HMAC algorithm here
# would let a caller sign their own tokens with the public key.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")


class KeySource:
    """Where signing keys come from. Two implementations: live JWKS, or static."""

    async def key_for(self, token: str) -> Any:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class JwksEndpoint(KeySource):
    """Production path: fetch and cache the issuer's JWKS over HTTPS."""

    uri: str
    _client: PyJWKClient | None = field(default=None, init=False, repr=False)

    async def key_for(self, token: str) -> Any:
        if self._client is None:
            # PyJWKClient caches keys by id and refetches on an unknown `kid`,
            # which is what makes signing-key rollover transparent.
            self._client = PyJWKClient(self.uri, cache_keys=True, lifespan=3600)
        client = self._client
        # PyJWKClient uses urllib and blocks. Off the event loop it goes, so a
        # slow or hung identity provider stalls one request rather than every
        # concurrent request the process is serving.
        signing_key = await anyio.to_thread.run_sync(client.get_signing_key_from_jwt, token)
        return signing_key.key


@dataclass
class StaticJwks(KeySource):
    """Development and test path: an in-memory JWKS, no network."""

    jwks: dict[str, Any]

    async def key_for(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        for key in self.jwks.get("keys", []):
            if kid is None or key.get("kid") == kid:
                return jwt.PyJWK(key).key
        raise jwt.PyJWKClientError(f"no key with kid {kid!r} in static JWKS")


class EntraTokenVerifier(TokenVerifier):
    """Validates Entra-issued JWTs and projects them onto `AccessToken`."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        key_source: KeySource,
        algorithms: tuple[str, ...] = ALLOWED_ALGORITHMS,
        leeway_seconds: int = 60,
    ) -> None:
        bad = set(algorithms) - set(ALLOWED_ALGORITHMS)
        if bad:
            raise ValueError(
                f"refusing to accept non-asymmetric or unknown algorithm(s): {', '.join(sorted(bad))}"
            )
        self.issuer = issuer
        self.audience = audience
        self.key_source = key_source
        self.algorithms = list(algorithms)
        # Small clock skew allowance between the identity provider and here.
        self.leeway_seconds = leeway_seconds

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an `AccessToken` for a valid token, or `None` for any failure.

        Returning `None` rather than raising is what the SDK's bearer backend
        expects, and it keeps the reason for rejection out of the HTTP response
        — the caller learns that the token was rejected, not which check
        failed. The detail goes to the log instead.
        """
        try:
            key = await self.key_source.key_for(token)
        except Exception as exc:
            logger.warning("token rejected: no usable signing key (%s)", exc)
            return None

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway_seconds,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.InvalidAudienceError:
            # Logged distinctly: a valid token aimed at another resource is a
            # different operational signal from an expired or forged one, and
            # is usually a misconfigured client rather than an attack.
            logger.warning("token rejected: audience is not %s", self.audience)
            return None
        except jwt.ExpiredSignatureError:
            logger.info("token rejected: expired")
            return None
        except jwt.InvalidTokenError as exc:
            logger.warning("token rejected: %s", exc)
            return None

        return self._to_access_token(token, claims)

    def _to_access_token(self, token: str, claims: dict[str, Any]) -> AccessToken:
        # Entra emits `scp` (space-delimited) for delegated flows, where a user
        # is present, and `roles` for app-only / client-credentials flows. Both
        # describe what was granted, so either can satisfy a required scope.
        scp = claims.get("scp")
        scopes = scp.split() if isinstance(scp, str) else list(scp or [])
        roles = _string_list(claims.get("roles"))
        if not scopes:
            scopes = list(roles)

        # `azp` is the modern claim; `appid` is what v1.0 Entra tokens carry.
        client_id = str(claims.get("azp") or claims.get("appid") or claims.get("sub") or "unknown")

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]) if "exp" in claims else None,
            resource=self.audience,
            subject=str(claims.get("sub")) if claims.get("sub") else None,
            # `claims` is what the policy layer reads roles from, and what the
            # audit log records the principal from.
            claims={
                "iss": claims.get("iss"),
                "roles": roles,
                "tid": claims.get("tid"),
                "oid": claims.get("oid"),
                "preferred_username": claims.get("preferred_username"),
            },
        )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def roles_from_token(access_token: AccessToken | None) -> list[str]:
    """The app roles a token carries, or an empty list if it carries none.

    Empty means "no roles", which under a fail-closed policy means no access —
    so an unauthenticated call and a call with a role-less token are treated
    identically, and neither is a special case in the enforcement path.
    """
    if access_token is None:
        return []
    return _string_list((access_token.claims or {}).get("roles"))


def describe_principal(access_token: AccessToken | None) -> str:
    """A stable, log-safe identifier for whoever is calling."""
    if access_token is None:
        return "anonymous"
    claims = access_token.claims or {}
    return str(claims.get("preferred_username") or access_token.subject or access_token.client_id)


def entra_issuer(tenant_id: str) -> str:
    """The v2.0 issuer URL for an Entra tenant."""
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def entra_jwks_uri(tenant_id: str) -> str:
    """The JWKS endpoint for an Entra tenant."""
    return f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


def load_jwks(raw: str) -> dict[str, Any]:
    """Parse a JWKS document supplied inline (used by the dev identity provider)."""
    return json.loads(raw)
