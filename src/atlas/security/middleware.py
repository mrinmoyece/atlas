"""HTTP hardening middleware.

Four concerns, all of which are standard in an enterprise service and all
of which are routinely missing from AI demos:

  * **Security headers.** CSP, HSTS, nosniff, frame-deny, referrer policy,
    and a restrictive permissions policy. Cheap, and their absence is the
    first thing a scanner reports.
  * **Body size limits.** Enforced from `Content-Length` *and* while
    streaming, because a chunked request can lie about its length. Without
    the streaming check the limit is decorative.
  * **Request correlation.** Every request gets an id, echoed in the
    response and attached to logs and audit entries, so one identifier ties
    together the API call, the graph trace and the audit record.
  * **Error containment.** Unhandled exceptions become a generic 500 with a
    request id. Stack traces in responses leak paths, versions and
    sometimes secrets.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from atlas.observability.logging import get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
MAX_BODY_BYTES = 1_000_000  # 1 MB; due-diligence requests are small JSON

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    # The API serves JSON only, so the strictest possible CSP applies.
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "Cache-Control": "no-store",
}


def apply_security_headers(response: Response) -> Response:
    """Stamp the security headers onto a response.

    Exposed as a function because error responses are constructed by the
    OUTERMOST middleware, which sits above the header middleware and would
    therefore skip it entirely. Every 413 and 500 shipped without CSP, HSTS
    or nosniff - precisely the responses an attacker is most likely to see.
    """
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        return apply_security_headers(await call_next(request))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Consume one request token before routing or body validation.

    Rate limiting cannot live in a route handler, and finding out why took a
    measurement. Placed in the handler it protects nothing that fails
    earlier: eight POSTs with an invalid `pattern` against a bucket sized
    for one returned eight 422s and zero 429s, because pydantic rejects the
    body before any handler code runs. A 422 is *cheaper* for an attacker to
    generate than a valid request, which makes it the better flood.

    Middleware runs before routing, so it covers 404s, 422s, 405s and
    unauthenticated probes alike. That is the correct scope: rate limiting
    protects the *server*, so it must apply to requests that turn out to be
    invalid. Spend limiting protects the *budget* and stays in the handler,
    after validation, where it only charges runs that will actually happen.

    Identity here is deliberately shallow - the presented key's id, or the
    client address when there is no valid key. Full authentication and RBAC
    still happen in the route dependency; duplicating them here would mean
    two places to get authorisation wrong.
    """

    def __init__(self, app, *, limiter, authenticator, protected_prefix: str = "/v1") -> None:
        super().__init__(app)
        self._limiter = limiter
        self._authenticator = authenticator
        self._prefix = protected_prefix

    def _identity(self, request: Request) -> str:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token:
            try:
                return self._authenticator.authenticate(token).subject
            except Exception:  # noqa: BLE001 - unauthenticated is not an error here
                pass
        # Anonymous and bad-key traffic is bucketed by source address, so one
        # attacker cannot exhaust a legitimate principal's bucket by guessing
        # keys - and cannot escape limiting by presenting none.
        client = request.client
        return f"anon:{client.host if client else 'unknown'}"

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(self._prefix):
            return await call_next(request)
        result = self._limiter.check(self._identity(request))
        if not result.allowed:
            return apply_security_headers(
                JSONResponse(
                    {"detail": result.reason},
                    status_code=429,
                    headers={"Retry-After": str(int(result.retry_after_s) or 1)},
                )
            )
        return await call_next(request)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id and converts unhandled errors into safe 500s."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except BodyTooLarge:
            response = apply_security_headers(
                JSONResponse(
                    {"error": "request body too large", "request_id": request_id},
                    status_code=413,
                )
            )
        except Exception:  # noqa: BLE001 - last line of defence
            log.exception(
                "unhandled_error",
                extra={"ctx": {"request_id": request_id, "path": request.url.path}},
            )
            response = apply_security_headers(
                JSONResponse(
                    {"error": "internal server error", "request_id": request_id},
                    status_code=500,
                )
            )
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class BodyTooLarge(Exception):
    """Raised when a streamed request body exceeds the limit."""


class BodySizeLimitMiddleware:
    """Rejects oversized bodies - written as **pure ASGI**, deliberately.

    The obvious implementation (a `BaseHTTPMiddleware` that buffers the body
    with `await request.body()` and replays it) is subtly broken: the
    replayed `receive` is attached to *that* middleware's Request object,
    while `call_next` passes its own disconnect-watching wrapper downstream.
    The replay therefore never reaches the endpoint, and instead gets
    consumed by the streaming-response disconnect listener, which raises
    "Unexpected message received: http.request" the moment you add SSE.

    Wrapping `receive` at the ASGI layer avoids the whole problem: the
    counter sits in the real message path, nothing is buffered, and memory
    stays bounded because the limit trips before the body is materialised.
    """

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            from atlas.security.middleware import apply_security_headers as _hdrs

            response = _hdrs(JSONResponse({"error": "request body too large"}, status_code=413))
            return await response(scope, receive, send)

        total = 0

        async def counting_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b"") or b"")
                if total > self.max_bytes:
                    raise BodyTooLarge()
            return message

        return await self.app(scope, counting_receive, send)
