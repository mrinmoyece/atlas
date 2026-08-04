"""Authentication and authorisation.

Deliberate choices, each of which is a question an enterprise security
review will ask:

* **Keys are never stored or compared in plaintext.** The service holds
  SHA-256 hashes; the presented key is hashed and compared with
  `hmac.compare_digest`. A `==` comparison leaks key material through
  timing, and storing raw keys turns a config leak into a full compromise.
* **Deny by default.** No key configured means every authenticated route
  returns 401 - not "open mode". A system that silently runs unauthenticated
  when misconfigured is how production gets exposed.
* **Roles are explicit and least-privilege.** `viewer` reads, `analyst`
  starts runs (spends money), `admin` manages configuration. Spending money
  is the privileged action in an agent platform, which is not obvious until
  you get the bill.
* **Principals are opaque in logs.** The key id is logged, never the key.

This is API-key auth with an OIDC-shaped seam: `Principal` carries `subject`
and `roles`, so swapping in JWT validation changes one function and nothing
downstream.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from enum import Enum

from pydantic import BaseModel, ConfigDict


class Role(str, Enum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"


# Role -> permissions it grants. Kept as data so an audit can read it.
ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset({"run:read"}),
    Role.ANALYST: frozenset({"run:read", "run:create"}),
    Role.ADMIN: frozenset({"run:read", "run:create", "admin:manage"}),
}


class Principal(BaseModel):
    """An authenticated caller."""

    model_config = ConfigDict(frozen=True)

    subject: str
    key_id: str = ""
    roles: tuple[Role, ...] = ()

    @property
    def permissions(self) -> frozenset[str]:
        out: set[str] = set()
        for role in self.roles:
            out |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(out)

    def can(self, permission: str) -> bool:
        return permission in self.permissions


class AuthError(Exception):
    """Raised for any failed authentication. The message is deliberately
    generic: telling a caller *why* a key failed helps an attacker
    enumerate valid key ids."""

    def __init__(self, message: str = "invalid or missing credentials") -> None:
        super().__init__(message)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


class ApiKeyAuthenticator:
    """Validates API keys against stored hashes.

    Configuration format (env `ATLAS_API_KEYS`):
        key_id:sha256hash:role[,role];key_id2:sha256hash2:role
    """

    def __init__(self, entries: dict[str, tuple[str, tuple[Role, ...]]] | None = None) -> None:
        self._entries = entries or {}

    @classmethod
    def from_env(cls, value: str | None = None) -> ApiKeyAuthenticator:
        raw = value if value is not None else os.environ.get("ATLAS_API_KEYS", "")
        entries: dict[str, tuple[str, tuple[Role, ...]]] = {}
        for chunk in filter(None, (c.strip() for c in raw.split(";"))):
            parts = chunk.split(":")
            if len(parts) < 3:
                continue
            key_id, digest, roles_raw = parts[0], parts[1], parts[2]
            roles = tuple(
                Role(r.strip())
                for r in roles_raw.split(",")
                if r.strip() in {r.value for r in Role}
            )
            if key_id and digest and roles:
                entries[key_id] = (digest.lower(), roles)
        return cls(entries)

    @property
    def configured(self) -> bool:
        return bool(self._entries)

    def authenticate(self, presented: str | None) -> Principal:
        """Validate a presented key. Constant-time, deny-by-default."""
        if not self.configured:
            # Misconfiguration must fail closed, never open.
            raise AuthError()
        if not presented:
            raise AuthError()

        candidate = hash_key(presented.strip())
        # Compare against every entry so the work is independent of which
        # key was presented - no early exit that could be timed.
        matched: tuple[str, tuple[Role, ...]] | None = None
        for key_id, (digest, roles) in self._entries.items():
            if hmac.compare_digest(candidate, digest):
                matched = (key_id, roles)
        if matched is None:
            raise AuthError()
        key_id, roles = matched
        return Principal(subject=f"apikey:{key_id}", key_id=key_id, roles=roles)


def require(principal: Principal, permission: str) -> None:
    if not principal.can(permission):
        raise PermissionError(f"principal lacks permission {permission!r}")
