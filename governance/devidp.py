"""A local stand-in for Microsoft Entra ID.

Mints RSA-signed tokens shaped like Entra v2.0 access tokens, and publishes the
matching JWKS in memory. That makes the whole authorization path — signature,
issuer, audience, expiry, roles — testable and demonstrable with no tenant, no
network and no secrets on disk.

The verifier does not know it is talking to this rather than to Entra: it is
handed a `KeySource`, and the only difference is where the keys came from.
Pointing the server at a real tenant is a configuration change, not a code
change, which is the property worth having.

Not for production use. The key is generated per process and lives in memory,
there is no authorization endpoint, and anyone who can call `issue()` can mint
any role they like — the point is to exercise the resource server, not to be
an identity provider.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

DEV_TENANT_ID = "00000000-0000-0000-0000-000000000000"
DEV_ISSUER = f"https://login.microsoftonline.local/{DEV_TENANT_ID}/v2.0"
DEV_AUDIENCE = "api://governed-mcp-server"
DEV_CLIENT_ID = "11111111-1111-1111-1111-111111111111"

# Where `--auth dev` keeps its signing key. Without a shared key on disk, the
# running server and a separate `--print-token` invocation each generate their
# own, and every token minted out-of-process is rejected — the demo looks like
# a bug in the verifier. Gitignored; regenerated on demand; worthless if leaked
# because it only signs tokens for a local development issuer.
DEV_KEY_PATH = Path(".devidp-key.pem")


@dataclass
class DevIdentityProvider:
    """Generates a signing key and issues Entra-shaped tokens against it.

    With `key_path`, the key is persisted so separate processes agree on it.
    Without one, the key is per-instance and in memory, which is what tests
    want — no shared state between them and nothing written to disk.
    """

    issuer: str = DEV_ISSUER
    audience: str = DEV_AUDIENCE
    key_path: Path | None = None
    key_id: str = field(default="", repr=False)
    _private_key: rsa.RSAPrivateKey = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._private_key = self._load_or_generate()
        if not self.key_id:
            # Derive the key id from the key itself, so any process loading the
            # same PEM advertises the same `kid` and JWKS lookup matches.
            public_bytes = self._private_key.public_key().public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            self.key_id = hashlib.sha256(public_bytes).hexdigest()[:32]

    def _load_or_generate(self) -> rsa.RSAPrivateKey:
        if self.key_path is not None and self.key_path.exists():
            loaded = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
            if not isinstance(loaded, rsa.RSAPrivateKey):
                raise TypeError(f"{self.key_path} does not contain an RSA private key")
            return loaded

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if self.key_path is not None:
            self.key_path.write_bytes(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            self.key_path.chmod(0o600)
        return key

    # -- key publication --------------------------------------------------

    def jwks(self) -> dict[str, Any]:
        """The public half, in JWKS form, as the issuer would publish it."""
        public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._private_key.public_key()))
        public_jwk.update({"kid": self.key_id, "use": "sig", "alg": "RS256"})
        return {"keys": [public_jwk]}

    # -- token issuance ---------------------------------------------------

    def issue(
        self,
        *,
        subject: str = "user@example.com",
        roles: list[str] | None = None,
        scopes: str = "mcp.invoke",
        audience: str | None = None,
        issuer: str | None = None,
        expires_in: int = 3600,
        algorithm: str = "RS256",
        key: Any = None,
        omit: tuple[str, ...] = (),
    ) -> str:
        """Mint a token.

        The last four arguments exist so tests can produce *invalid* tokens —
        wrong audience, wrong issuer, expired, unsigned, signed with the wrong
        key — without reaching into the JWT library. A verifier is only as
        trustworthy as the bad tokens it has been shown to reject.
        """
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer or self.issuer,
            "aud": audience or self.audience,
            "sub": subject,
            "oid": uuid.uuid5(uuid.NAMESPACE_URL, subject).hex,
            "tid": DEV_TENANT_ID,
            "azp": DEV_CLIENT_ID,
            "preferred_username": subject,
            "iat": now,
            "nbf": now,
            "exp": now + expires_in,
            "roles": roles if roles is not None else [],
            "scp": scopes,
        }
        for claim in omit:
            claims.pop(claim, None)

        headers = {"kid": self.key_id}
        signing_key = key if key is not None else self._private_key
        if algorithm == "none":
            # PyJWT requires an explicit opt-in to produce an unsigned token.
            return jwt.encode(claims, key=None, algorithm=None, headers=headers)  # type: ignore[arg-type]
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers=headers)

    def expired(self, **kwargs: Any) -> str:
        """A token that was valid an hour ago. Convenience for tests."""
        kwargs.setdefault("expires_in", -3600)
        return self.issue(**kwargs)
