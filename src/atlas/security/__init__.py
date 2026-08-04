"""Enterprise security controls.

Threat model in one paragraph: Atlas accepts requests from clients that may
be compromised, analyses repositories whose contents are attacker-authored,
and spends money on every run. The controls therefore split into three
groups - *who is calling* (auth.py), *how much they may do* (ratelimit.py),
and *what we can prove afterwards* (audit.py) - with transport hardening in
middleware.py. Prompt-injection defences for untrusted repository content
live with the agents (prompts, tool allowlists, output validation), because
that is where they are enforceable.
"""

from atlas.security.audit import AuditEntry, AuditLog
from atlas.security.auth import (
    ApiKeyAuthenticator,
    AuthError,
    Principal,
    Role,
    hash_key,
    require,
)
from atlas.security.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    apply_security_headers,
)
from atlas.security.ratelimit import RateLimiter, RateLimitResult

__all__ = [
    "AuditEntry",
    "AuditLog",
    "ApiKeyAuthenticator",
    "AuthError",
    "Principal",
    "Role",
    "hash_key",
    "require",
    "BodySizeLimitMiddleware",
    "apply_security_headers",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "RateLimiter",
    "RateLimitResult",
]
